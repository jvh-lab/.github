"""Main-push-vahti: jokaisen mainiin tulevan commitin on tultava yhdistetyn PR:n kautta.

Jälkitarkistus (agent-foundation D023 J2). Ajetaan GitHub Actionsissa, kun mainiin pushataan.
Vain vakiokirjasto. Paluukoodit: 0 = kunnossa tai ei tarkistettavaa, 1 = suora push havaittu,
2 = tarkistus ei valmistunut (API-virhe tai puutteellinen ympäristö).

Kaksi ehtoa:
1. Jokaisella pushin commitilla on PR-kytkentä (GET /repos/{owner}/{repo}/commits/{sha}/pulls)
   PR:ään, joka on yhdistetty (merged_at) ja jonka kohde on main.
2. Mainin ensimmäisen vanhemman ketjun jokainen uusi commit on GitHubin luoma (committer
   web-flow ja varmennettu allekirjoitus). Ehto 1 ei yksin riitä: GitHub merkitsee PR:n
   yhdistetyksi myös, kun sen haara pushataan suoraan mainiin (epäsuora yhdistäminen).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAIN_BRANCH = "main"
MAIN_REF = "refs/heads/main"
WORKFLOW_FILE = "main-push-vahti.yml"
GITHUB_COMMITTER = "web-flow"
BOT_LOGIN = "github-actions[bot]"
ISSUE_TITLE = "Suora push mainiin"
API_VERSION = "2026-03-10"
PAGE_SIZE = 100
MAX_COMMITS = 500
MAX_ISSUE_PAGES = 10
# Kytkentä voi näkyä rajapinnassa vasta hetken yhdistämisen jälkeen, joten puuttuvat
# tarkistetaan uudelleen. Uusinta tehdään vain, kun kärki on GitHubin luoma yhdistäminen:
# muuten push on joka tapauksessa hälytys, eikä odottaminen muuta tulosta.
RETRY_DELAYS = (10, 20, 30, 60)
RETRY_LIMIT = 100
MAX_RATE_WAIT = 60
LISTED_COMMITS = 50
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Sleep = Callable[[float], None]


class ApiError(Exception):
    """GitHubin rajapintakutsu epäonnistui. status 0 = verkko-, aikakatkaisu- tai muotovirhe."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"GitHub API {status or 'verkko- tai muotovirhe'}: {path}")
        self.status = status
        self.path = path


def rate_limit_wait(status: int, headers: Any) -> float | None:
    """Odotusaika sekunteina, jos vastaus on kiintiörajoitus; muuten None."""
    if status not in (403, 429) or headers is None:
        return None
    retry_after = headers.get("retry-after")
    if retry_after is None and headers.get("x-ratelimit-remaining") != "0":
        return None
    try:
        wait = float(retry_after) if retry_after is not None else MAX_RATE_WAIT
    except ValueError:
        wait = MAX_RATE_WAIT
    return min(max(wait, 1.0), MAX_RATE_WAIT)


class GitHubApi:
    """Pieni REST-asiakas yhteen repoon. Tunnusta ei tulosteta eikä tallenneta."""

    def __init__(
        self, base_url: str, repo: str, credential: str, sleep: Sleep = time.sleep
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/repos/{repo}"
        self._credential = credential
        self._sleep = sleep

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        # Ohimenevä virhe (verkko, 5xx tai kiintiöraja) uusitaan kahdesti; muut palautetaan heti.
        delay: float = 0
        status = 0
        for attempt in range(3):
            if delay:
                self._sleep(delay)
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", API_VERSION)
            req.add_header("Authorization", f"Bearer {self._credential}")
            req.add_header("User-Agent", "main-push-vahti")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as err:
                status = err.code
                wait = rate_limit_wait(status, err.headers)
                if wait is None and status < 500:
                    raise ApiError(status, path) from None
                delay = wait if wait is not None else (2, 5)[min(attempt, 1)]
            except (OSError, http.client.HTTPException):
                status = 0
                delay = (2, 5)[min(attempt, 1)]
            else:
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    raise ApiError(0, path) from None
        raise ApiError(status, path)


@dataclass
class Result:
    status: str  # ok, skip, violation tai error
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    commits: list[dict[str, str]] = field(default_factory=list)
    outside: list[dict[str, str]] = field(default_factory=list)
    checked: int = 0
    base: str = ""

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "skip": 0, "violation": 1}.get(self.status, 2)


def is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def merged_main_prs(prs: Any) -> list[int]:
    """Palauttaa PR:t, jotka on yhdistetty ja joiden kohde on main."""
    numbers = []
    for pr in prs if isinstance(prs, list) else []:
        base = pr.get("base") or {}
        if pr.get("merged_at") and base.get("ref") == MAIN_BRANCH:
            numbers.append(int(pr["number"]))
    return sorted(numbers)


def compare_commits(api: GitHubApi, base: str, head: str) -> tuple[str, int, list[dict[str, Any]]]:
    """Lukee vertailun base...head kaikki commitit sivuittain (enintään MAX_COMMITS)."""
    status, total, commits = "", 0, []
    page = 1
    while True:
        data = api.request(
            "GET", f"/compare/{base}...{head}", {"per_page": PAGE_SIZE, "page": page}
        )
        status = str(data.get("status", ""))
        total = int(data.get("total_commits", 0))
        batch = data.get("commits") or []
        commits.extend(batch)
        if total > MAX_COMMITS or len(commits) >= total or not batch:
            return status, total, commits
        page += 1


def previous_checked_head(api: GitHubApi, run_id: str) -> str:
    """Edellisen valmistuneen vahtiajon kärki mainissa, tai tyhjä, jos ajoa ei ole."""
    data = api.request(
        "GET",
        f"/actions/workflows/{WORKFLOW_FILE}/runs",
        {"branch": MAIN_BRANCH, "event": "push", "status": "completed", "per_page": 30},
    )
    runs = [
        run
        for run in (data or {}).get("workflow_runs") or []
        if str(run.get("id")) != run_id and run.get("conclusion") in ("success", "failure")
    ]
    runs.sort(key=lambda run: int(run.get("run_number") or 0), reverse=True)
    return str(runs[0].get("head_sha") or "") if runs else ""


def choose_base(api: GitHubApi, before: str, run_id: str, notes: list[str]) -> str:
    """Vertailupohja: edellisen vahtiajon kärki, jos välistä puuttuu ajo; muuten before.

    Push, jonka työnkulku ohitettiin ([skip ci]) tai jonka ajo peruttiin, tarkistetaan näin
    seuraavassa ajossa.
    """
    try:
        previous = previous_checked_head(api, run_id)
        if not previous or previous == before or not SHA_RE.fullmatch(previous):
            return before
        status = str(api.request("GET", f"/compare/{previous}...{before}").get("status", ""))
    except ApiError as err:
        notes.append(f"Edellisten vahtiajojen luku epäonnistui ({err}); vertailupohja on before.")
        return before
    if status == "ahead":
        notes.append(
            f"Edellinen vahtiajo tarkisti kärjen {previous[:12]}. Väli siitä tämän pushin "
            "before-arvoon jäi ilman vahtiajoa (esim. [skip ci] tai peruttu ajo), joten se "
            "tarkistetaan nyt."
        )
        return previous
    notes.append(
        f"Edellisen vahtiajon kärki {previous[:12]} ei ole before-arvon esivanhempi; "
        "vertailupohja on before."
    )
    return before


def describe(commit: dict[str, Any]) -> dict[str, str]:
    info = commit.get("commit") or {}
    message = str(info.get("message") or "")
    title = message.splitlines()[0] if message else ""
    author = str((info.get("author") or {}).get("name") or "")
    return {"sha": str(commit.get("sha", "")), "title": title, "author": author}


def created_by_github(commit: dict[str, Any]) -> bool:
    committer = (commit.get("committer") or {}).get("login")
    verification = (commit.get("commit") or {}).get("verification") or {}
    return committer == GITHUB_COMMITTER and verification.get("verified") is True


def first_parent_path(
    api: GitHubApi, commits: list[dict[str, Any]], base: str, after: str
) -> tuple[list[dict[str, Any]], bool]:
    """Ketju after → base ensimmäisiä vanhempia pitkin vertailun commiteista.

    Palauttaa ketjun commitit ja tiedon, päättyykö ketju vertailupohjaan.
    """
    by_sha = {str(c.get("sha")): c for c in commits}
    path: list[dict[str, Any]] = []
    current = after
    while current in by_sha and len(path) < len(by_sha):
        commit = by_sha[current]
        if "verification" not in (commit.get("commit") or {}):
            commit = api.request("GET", f"/commits/{current}") or commit
        path.append(commit)
        parents = commit.get("parents") or by_sha[current].get("parents") or []
        current = str(parents[0].get("sha")) if parents else ""
    return path, current == base


def check_push(
    event: dict[str, Any],
    api: GitHubApi,
    sleep: Sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
    run_id: str = "",
) -> Result:
    ref = str(event.get("ref") or "")
    if ref != MAIN_REF:
        return Result(
            "skip", notes=[f"Viite {ref or '(puuttuu)'} ei ole {MAIN_REF}; ei tarkistettavaa."]
        )
    before = str(event.get("before") or "")
    after = str(event.get("after") or "")
    if event.get("deleted") or is_zero(after):
        return Result("skip", notes=["Main poistettiin; tarkistettavia committeja ei ole."])
    if not SHA_RE.fullmatch(after):
        return Result("error", notes=["Tapahtuman after-arvo ei ole commit-tunniste."])
    if event.get("created") or is_zero(before):
        return Result(
            "skip",
            notes=[
                "Main luotiin tässä pushissa (esimerkiksi uuden repon ensimmäinen push), "
                "joten vertailupohjaa ei ole. Seuraavat pushit tarkistetaan."
            ],
        )
    if not SHA_RE.fullmatch(before):
        return Result("error", notes=["Tapahtuman before-arvo ei ole commit-tunniste."])

    result = Result("ok")
    forced = bool(event.get("forced"))
    if forced:
        result.reasons.append("Pakotettu push: mainin historia kirjoitettiin uudelleen.")
    base = before if forced else choose_base(api, before, run_id, result.notes)
    result.base = base
    try:
        status, total, commits = compare_commits(api, base, after)
    except ApiError as err:
        if result.reasons:
            result.status = "violation"
            result.notes.append(f"Vertailu epäonnistui ({err}); committeja ei tarkistettu.")
            return result
        result.status = "error"
        result.notes.append(f"Vertailu epäonnistui: {err}")
        return result
    if status not in ("ahead", "identical") and not forced:
        result.reasons.append(f"Vertailun tila on {status}: main ei edennyt suoraan eteenpäin.")
    if total > MAX_COMMITS:
        result.reasons.append(
            f"Pushissa on {total} committia, yli tarkistusrajan {MAX_COMMITS}. Tarkista käsin."
        )
        result.status = "violation"
        return result

    try:
        path, reaches_base = first_parent_path(api, commits, base, after)
        result.outside = [describe(c) for c in path if not created_by_github(c)]
        if result.outside:
            result.reasons.append(
                f"{len(result.outside)} mainin ensimmäisen vanhemman ketjun commitia ei ole "
                "GitHubin luoma yhdistäminen (committer ei ole GitHub tai allekirjoitusta ei "
                "ole varmennettu). PR-haaran suora push merkitsee PR:n yhdistetyksi, mutta "
                "ohittaa yhdistämisen."
            )
        if not reaches_base and not forced:
            result.reasons.append(
                "Mainin ensimmäisen vanhemman ketju ei palaa edelliseen kärkeen, joten main ei "
                "edennyt PR-yhdistämisellä."
            )
        merge_like = not result.reasons

        pending = [c for c in commits if c.get("sha")]
        result.checked = len(pending)
        for attempt, delay in enumerate((0, *delays)):
            if attempt and (not merge_like or len(pending) > RETRY_LIMIT):
                break
            if delay:
                sleep(delay)
            missing = []
            for commit in pending:
                prs = api.request("GET", f"/commits/{commit['sha']}/pulls", {"per_page": PAGE_SIZE})
                if not merged_main_prs(prs):
                    missing.append(commit)
            pending = missing
            if not pending:
                break
    except ApiError as err:
        # Jo todettu syy (esim. pakotettu push) hälyttää, vaikka loppu jäi tarkistamatta.
        result.status = "violation" if result.reasons else "error"
        result.notes.append(f"Ketjun tai PR-kytkennän luku epäonnistui: {err}")
        return result

    if pending:
        result.reasons.append(
            f"{len(pending)} commitilla ei ole yhdistettyä PR:ää, jonka kohde on {MAIN_BRANCH}."
        )
        result.commits = [describe(c) for c in pending]
    if result.reasons:
        result.status = "violation"
    return result


def code_span(text: str, limit: int = 100) -> str:
    """Koodimerkintä estää @-maininnat ja Markdown-tulkinnan. Takahipsut korvataan."""
    clean = text.replace("`", "'").replace("\r", " ").replace("\n", " ").strip()
    if len(clean) > limit:
        clean = clean[: limit - 1] + "…"
    return f"`{clean}`" if clean else "`-`"


def commit_lines(heading: str, commits: list[dict[str, str]]) -> list[str]:
    if not commits:
        return []
    lines = ["", heading]
    for commit in commits[:LISTED_COMMITS]:
        lines.append(
            f"- {code_span(commit['sha'][:12])} {code_span(commit['title'])} "
            f"({code_span(commit['author'], 60)})"
        )
    if len(commits) > LISTED_COMMITS:
        lines.append(f"- … ja {len(commits) - LISTED_COMMITS} muuta (ks. ajo).")
    return lines


def report_body(event: dict[str, Any], result: Result, repo: str, run_url: str) -> str:
    before = str(event.get("before") or "")[:12]
    after = str(event.get("after") or "")[:12]
    pusher = str((event.get("pusher") or {}).get("name") or "")
    if result.status == "violation":
        intro = (
            "Main-push-vahti havaitsi mainissa muutoksen, joka ei tullut yhdistetyn pull "
            "requestin kautta."
        )
    else:
        intro = (
            "Main-push-vahdin tarkistus ei valmistunut, joten tätä pushia ei ole todennettu. "
            "Tarkista se käsin ja aja vahti uudelleen."
        )
    lines = [
        intro,
        "",
        f"- Repo: {code_span(repo)}",
        f"- Push: {code_span(before)} → {code_span(after)}",
        f"- Pushaaja: {code_span(pusher)}",
        f"- Ajo: {run_url}",
    ]
    if result.base and result.base != str(event.get("before") or ""):
        lines.append(f"- Vertailupohja: {code_span(result.base[:12])}")
    compare_url = str(event.get("compare") or "")
    if compare_url.startswith("https://"):
        lines.append(f"- Vertailu: {compare_url}")
    lines += ["", "Syyt:"] + [f"- {text}" for text in result.reasons + result.notes]
    lines += commit_lines("Mainin ketjun commitit, joita GitHub ei luonut:", result.outside)
    lines += commit_lines(
        f"Commitit ilman yhdistettyä PR:ää (kohde {MAIN_BRANCH}):", result.commits
    )
    lines += [
        "",
        "Selvitä, kuka pushasi ja miksi. Palauta muutos tarvittaessa revert-PR:llä ja sulje tämä "
        "issue, kun asia on käsitelty. Uudet havainnot lisätään tähän kommentteina, kunnes issue "
        "suljetaan.",
    ]
    return "\n".join(lines) + "\n"


def report_issue(api: GitHubApi, body: str) -> str:
    """Kommentoi vahdin avointa issueta tai avaa uuden. Yksi avoin issue kerrallaan.

    Vain vahdin (github-actions[bot]) avaamat issuet kelpaavat, jotta kuka tahansa ei voi
    ohjata hälytyksiä omaan samannimiseen issueensa.
    """
    existing: list[int] = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        issues = api.request(
            "GET", "/issues", {"state": "open", "per_page": PAGE_SIZE, "page": page}
        )
        for issue in issues or []:
            if (
                issue.get("title") == ISSUE_TITLE
                and not issue.get("pull_request")
                and (issue.get("user") or {}).get("login") == BOT_LOGIN
            ):
                existing.append(int(issue["number"]))
        if len(issues or []) < PAGE_SIZE:
            break
    if existing:
        number = min(existing)
        api.request("POST", f"/issues/{number}/comments", body={"body": body})
        return f"Lisätty kommentti issueen #{number}."
    created = api.request("POST", "/issues", body={"title": ISSUE_TITLE, "body": body})
    return f"Avattu issue #{(created or {}).get('number', '?')}."


def summary_text(result: Result, extra: list[str]) -> str:
    heading = {
        "ok": "Kunnossa: jokainen commit tuli yhdistetyn PR:n kautta, jonka kohde on main.",
        "skip": "Ei tarkistettavaa.",
        "violation": "HÄLYTYS: suora push mainiin.",
    }.get(result.status, "Tarkistus ei valmistunut.")
    lines = [f"## Main-push-vahti: {heading}", "", f"Tarkistettuja committeja: {result.checked}"]
    lines += [f"- {text}" for text in result.reasons + result.notes + extra]
    for commit in (result.outside + result.commits)[:LISTED_COMMITS]:
        lines.append(f"- {code_span(commit['sha'][:12])} {code_span(commit['title'])}")
    return "\n".join(lines) + "\n"


def run(
    event: dict[str, Any],
    api: GitHubApi,
    sleep: Sleep,
    repo: str,
    run_url: str,
    summary_path: str | None = None,
    delays: tuple[float, ...] = RETRY_DELAYS,
    run_id: str = "",
) -> int:
    result = check_push(event, api, sleep, delays, run_id)
    extra: list[str] = []
    if result.status in ("violation", "error") and str(event.get("ref") or "") == MAIN_REF:
        try:
            extra.append(report_issue(api, report_body(event, result, repo, run_url)))
        except ApiError as err:
            extra.append(f"Issuen avaus tai päivitys epäonnistui: {err}")
    # Jokainen rivi alkaa omalla etuliitteellä, joten commit-otsikko ei voi olla työnkulkukomento.
    print(f"main-push-vahti: {result.status}, tarkistettuja committeja {result.checked}")
    for text in result.reasons + result.notes + extra:
        print(f"main-push-vahti: {text}")
    for commit in result.outside:
        print(f"main-push-vahti: ei GitHubin luoma {commit['sha'][:12]} {commit['title'][:100]}")
    for commit in result.commits:
        print(f"main-push-vahti: ilman PR:ää {commit['sha'][:12]} {commit['title'][:100]}")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary_text(result, extra))
    return result.exit_code


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    credential = os.environ.get("GITHUB_TOKEN", "")
    if not (event_path and repo and credential):
        print("main-push-vahti: GITHUB_EVENT_PATH, GITHUB_REPOSITORY tai GITHUB_TOKEN puuttuu")
        return 2
    try:
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_url = f"{server}/{repo}/actions/runs/{run_id}"
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        api = GitHubApi(api_url, repo, credential)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        return run(event, api, time.sleep, repo, run_url, summary, RETRY_DELAYS, run_id)
    except Exception as err:  # Odottamaton virhe ei saa näyttää hälytykseltä (paluukoodi 1).
        print(f"main-push-vahti: odottamaton virhe {type(err).__name__}; tarkistus ei valmistunut")
        return 2


if __name__ == "__main__":
    sys.exit(main())
