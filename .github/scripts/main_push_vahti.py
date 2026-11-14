"""Main-push-vahti: jokaisen mainiin tulevan commitin on tultava yhdistetyn PR:n kautta.

Jälkitarkistus (agent-foundation D023 J2). Ajetaan GitHub Actionsissa, kun mainiin pushataan.
Vain vakiokirjasto. Paluukoodit: 0 = kunnossa tai ei tarkistettavaa, 1 = suora push havaittu,
2 = tarkistus ei valmistunut (API-virhe tai puutteellinen ympäristö).

Commitin PR-kytkentä luetaan rajapinnasta GET /repos/{owner}/{repo}/commits/{sha}/pulls.
Hyväksytty kytkentä on PR, joka on yhdistetty (merged_at) ja jonka kohde on main.
"""

from __future__ import annotations

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
ISSUE_TITLE = "Suora push mainiin"
API_VERSION = "2026-03-10"
PAGE_SIZE = 100
MAX_COMMITS = 500
MAX_ISSUE_PAGES = 10
# Kytkentä voi näkyä rajapinnassa vasta hetken yhdistämisen jälkeen, joten puuttuvat
# tarkistetaan uudelleen. Uusinta tehdään vain pienelle joukolle: suora push suuresta
# joukosta ei korjaannu odottamalla, eikä sen pidä kuluttaa rajapintakiintiötä.
RETRY_DELAYS = (10, 20, 30, 60)
RETRY_LIMIT = 25
LISTED_COMMITS = 50
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Sleep = Callable[[float], None]


class ApiError(Exception):
    """GitHubin rajapintakutsu epäonnistui. status 0 = verkko- tai aikakatkaisuvirhe."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"GitHub API {status or 'verkkovirhe'}: {path}")
        self.status = status
        self.path = path


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
        # Ohimenevä virhe (verkko tai 5xx) uusitaan kahdesti; muut virheet palautetaan heti.
        for attempt, delay in enumerate((0, 2, 5)):
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
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                status = err.code
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                status = 0
            if (status and status < 500) or attempt == 2:
                raise ApiError(status, path)
        raise AssertionError("unreachable")


@dataclass
class Result:
    status: str  # ok, skip, violation tai error
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    commits: list[dict[str, str]] = field(default_factory=list)
    checked: int = 0

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


def compare_commits(
    api: GitHubApi, before: str, after: str
) -> tuple[str, int, list[dict[str, Any]]]:
    """Lukee vertailun before...after kaikki commitit sivuittain (enintään MAX_COMMITS)."""
    status, total, commits = "", 0, []
    page = 1
    while True:
        data = api.request(
            "GET", f"/compare/{before}...{after}", {"per_page": PAGE_SIZE, "page": page}
        )
        status = str(data.get("status", ""))
        total = int(data.get("total_commits", 0))
        batch = data.get("commits") or []
        commits.extend(batch)
        if total > MAX_COMMITS or len(commits) >= total or not batch:
            return status, total, commits
        page += 1


def describe(commit: dict[str, Any]) -> dict[str, str]:
    info = commit.get("commit") or {}
    message = str(info.get("message") or "")
    title = message.splitlines()[0] if message else ""
    author = str((info.get("author") or {}).get("name") or "")
    return {"sha": str(commit.get("sha", "")), "title": title, "author": author}


def check_push(
    event: dict[str, Any],
    api: GitHubApi,
    sleep: Sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
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
    if event.get("forced"):
        result.reasons.append("Pakotettu push: mainin historia kirjoitettiin uudelleen.")
    try:
        status, total, commits = compare_commits(api, before, after)
    except ApiError as err:
        if result.reasons:
            result.status = "violation"
            result.notes.append(f"Vertailu epäonnistui ({err}); committeja ei tarkistettu.")
            return result
        return Result("error", notes=[f"Vertailu epäonnistui: {err}"])
    if status not in ("ahead", "identical") and not event.get("forced"):
        result.reasons.append(f"Vertailun tila on {status}: main ei edennyt suoraan eteenpäin.")
    if total > MAX_COMMITS:
        result.reasons.append(
            f"Pushissa on {total} committia, yli tarkistusrajan {MAX_COMMITS}. Tarkista käsin."
        )
        result.status = "violation"
        return result

    pending = [c for c in commits if c.get("sha")]
    result.checked = len(pending)
    try:
        for attempt, delay in enumerate((0, *delays)):
            if attempt and len(pending) > RETRY_LIMIT:
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
        return Result("error", notes=[f"PR-kytkennän luku epäonnistui: {err}"])

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


def report_body(event: dict[str, Any], result: Result, repo: str, run_url: str) -> str:
    before = str(event.get("before") or "")[:12]
    after = str(event.get("after") or "")[:12]
    pusher = str((event.get("pusher") or {}).get("name") or "")
    lines = [
        "Main-push-vahti havaitsi mainissa muutoksen, joka ei tullut yhdistetyn pull requestin "
        "kautta.",
        "",
        f"- Repo: {code_span(repo)}",
        f"- Push: {code_span(before)} → {code_span(after)}",
        f"- Pushaaja: {code_span(pusher)}",
        f"- Ajo: {run_url}",
    ]
    compare_url = str(event.get("compare") or "")
    if compare_url.startswith("https://"):
        lines.append(f"- Vertailu: {compare_url}")
    lines += ["", "Syyt:"] + [f"- {reason}" for reason in result.reasons]
    lines += [f"- {note}" for note in result.notes]
    if result.commits:
        lines += ["", f"Commitit ilman yhdistettyä PR:ää (kohde {MAIN_BRANCH}):"]
        for commit in result.commits[:LISTED_COMMITS]:
            lines.append(
                f"- {code_span(commit['sha'][:12])} {code_span(commit['title'])} "
                f"({code_span(commit['author'], 60)})"
            )
        if len(result.commits) > LISTED_COMMITS:
            lines.append(f"- … ja {len(result.commits) - LISTED_COMMITS} muuta (ks. ajo).")
    lines += [
        "",
        "Selvitä, kuka pushasi ja miksi. Palauta muutos tarvittaessa revert-PR:llä ja sulje tämä "
        "issue, kun asia on käsitelty. Uudet havainnot lisätään tähän kommentteina, kunnes issue "
        "suljetaan.",
    ]
    return "\n".join(lines) + "\n"


def report_issue(api: GitHubApi, body: str) -> str:
    """Kommentoi avointa Suora push mainiin -issueta tai avaa uuden. Yksi avoin issue kerrallaan."""
    existing: list[int] = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        issues = api.request(
            "GET", "/issues", {"state": "open", "per_page": PAGE_SIZE, "page": page}
        )
        for issue in issues or []:
            if issue.get("title") == ISSUE_TITLE and "pull_request" not in issue:
                existing.append(int(issue["number"]))
        if len(issues or []) < PAGE_SIZE:
            break
    if existing:
        number = min(existing)
        api.request("POST", f"/issues/{number}/comments", body={"body": body})
        return f"Lisätty kommentti issueen #{number}."
    created = api.request("POST", "/issues", body={"title": ISSUE_TITLE, "body": body})
    return f"Avattu issue #{created['number']}."


def summary_text(result: Result, extra: list[str]) -> str:
    heading = {
        "ok": "Kunnossa: jokaisella commitilla on yhdistetty PR, jonka kohde on main.",
        "skip": "Ei tarkistettavaa.",
        "violation": "HÄLYTYS: suora push mainiin.",
    }.get(result.status, "Tarkistus ei valmistunut.")
    lines = [f"## Main-push-vahti: {heading}", "", f"Tarkistettuja committeja: {result.checked}"]
    lines += [f"- {text}" for text in result.reasons + result.notes + extra]
    for commit in result.commits[:LISTED_COMMITS]:
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
) -> int:
    result = check_push(event, api, sleep, delays)
    extra: list[str] = []
    if result.status == "violation":
        try:
            extra.append(report_issue(api, report_body(event, result, repo, run_url)))
        except ApiError as err:
            extra.append(f"Issuen avaus tai päivitys epäonnistui: {err}")
    # Jokainen rivi alkaa omalla etuliitteellä, joten commit-otsikko ei voi olla työnkulkukomento.
    print(f"main-push-vahti: {result.status}, tarkistettuja committeja {result.checked}")
    for text in result.reasons + result.notes + extra:
        print(f"main-push-vahti: {text}")
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
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_url = f"{server}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    api = GitHubApi(os.environ.get("GITHUB_API_URL", "https://api.github.com"), repo, credential)
    return run(event, api, time.sleep, repo, run_url, os.environ.get("GITHUB_STEP_SUMMARY"))


if __name__ == "__main__":
    sys.exit(main())
