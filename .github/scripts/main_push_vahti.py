"""Main-push-vahti: jokaisen mainiin tulevan commitin on tultava yhdistetyn PR:n kautta.

Jälkitarkistus (agent-foundation D023 J2). Ajetaan GitHub Actionsissa, kun mainiin
pushataan. Vain vakiokirjasto. Paluukoodit: 0 = kunnossa tai ei tarkistettavaa,
1 = suora push havaittu, 2 = tarkistus ei valmistunut (myös, jos samalla havaittiin
rikkomus; hälytys raportoidaan silti), 3 = tämä push tarkistettiin,
mutta edellistä tarkistuspistettä ei löytynyt (aiempi väli todentamatta).

Kolme ehtoa:
1. Jokaisella pushin commitilla on PR-kytkentä
   (GET /repos/{owner}/{repo}/commits/{sha}/pulls) PR:ään, joka on yhdistetty
   (merged_at) ja jonka kohde on main.
2. Mainin ensimmäisen vanhemman ketjun jokainen uusi commit on GitHubin luoma
   (committer web-flow ja varmennettu allekirjoitus). Ehto 1 ei yksin riitä: GitHub
   merkitsee PR:n yhdistetyksi myös, kun sen haara pushataan suoraan mainiin.
3. Mikään mainin ensimmäisen vanhemman ketjun uusi commit ei ole siihen kytketyn
   PR:n oma head-commit. Aito yhdistäminen (merge, squash tai rebase) luo aina uuden
   commitin, mutta PR-haaran suora push tuo ketjuun PR:n headin, vaikka sen commitit
   olisi tehty GitHubin verkkoeditorilla.

Vertailupohja on edellisen tarkistuspisteen kärki: ajon, jonka tarkistus-vaihe
valmistui. Ohitettu, peruttu tai ennen tarkistusta kaatunut ajo ei siis jätä aukkoa,
ja historian muutos ajojen välissä hälyttää. GitHub Actionsissa valmis tarkistus
palauttaa 0 ja kirjoittaa tuloksen (tulos=ok, tulos=violation tai tulos=gap)
GITHUB_OUTPUTiin; erillinen vaihe kaataa ajon hälytyksestä.
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
CHECK_STEP = "tarkistus"
MAX_CHECKPOINT_LOOKUPS = 10
GITHUB_COMMITTER = "web-flow"
BOT_LOGIN = "github-actions[bot]"
ISSUE_TITLE = "Suora push mainiin"
API_VERSION = "2026-03-10"
PAGE_SIZE = 100
MAX_COMMITS = 500
MAX_ISSUE_PAGES = 10
# Kytkentä voi näkyä rajapinnassa vasta hetken yhdistämisen jälkeen, joten puuttuvat
# tarkistetaan uudelleen. Uusinta tehdään vain, kun kärki on GitHubin luoma
# yhdistäminen: muuten push on joka tapauksessa hälytys.
RETRY_DELAYS = (10, 20, 30, 60)
RETRY_LIMIT = 100
MAX_RATE_WAIT = 60
LISTED_COMMITS = 50
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Sleep = Callable[[float], None]


class NoCheckpoint(Exception):
    """Ajoja on, mutta yksikään viimeisistä ei kelpaa tarkistuspisteeksi."""


class ApiError(Exception):
    """Rajapintakutsu epäonnistui. status 0 = verkko-, aikakatkaisu- tai muotovirhe."""

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
        self,
        base_url: str,
        repo: str,
        credential: str,
        sleep: Sleep = time.sleep,
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
        # Luku uusitaan kahdesti ohimenevän virheen (verkko, 5xx) jälkeen. Kirjoitusta
        # ei uusita, koska GitHub on voinut jo luoda issuen; kiintiöraja uusitaan aina.
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
                if wait is None and (status < 500 or method != "GET"):
                    raise ApiError(status, path) from None
                delay = wait if wait is not None else (2, 5)[min(attempt, 1)]
            except (OSError, http.client.HTTPException):
                if method != "GET":
                    raise ApiError(0, path) from None
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
    status: str  # ok, skip, violation, gap tai error
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    gap: list[str] = field(default_factory=list)
    commits: list[dict[str, str]] = field(default_factory=list)
    outside: list[dict[str, str]] = field(default_factory=list)
    checked: int = 0
    base: str = ""

    @property
    def exit_code(self) -> int:
        # Keskeneräinen tarkistus on aina 2, vaikka rikkomus havaittiin: hälytys
        # raportoidaan, mutta ajo ei saa kelvata seuraavan ajon tarkistuspisteeksi.
        if self.incomplete:
            return 2
        return {"ok": 0, "skip": 0, "violation": 1, "gap": 3}.get(self.status, 2)

    def settle(self) -> Result:
        """Tila: hälytys voittaa keskeneräisyyden (paluukoodi on silti 2, ks. exit_code)."""
        if self.reasons:
            self.status = "violation"
        elif self.incomplete:
            self.status = "error"
        elif self.gap:
            self.status = "gap"
        return self


def is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def merged_main_prs(prs: Any) -> list[dict[str, Any]]:
    """Palauttaa PR:t, jotka on yhdistetty ja joiden kohde on main."""
    merged = []
    for pr in prs if isinstance(prs, list) else []:
        base = pr.get("base") or {}
        if pr.get("merged_at") and base.get("ref") == MAIN_BRANCH:
            merged.append(pr)
    return merged


def head_sha(pr: dict[str, Any]) -> str:
    return str((pr.get("head") or {}).get("sha") or "")


def compare_commits(
    api: GitHubApi,
    base: str,
    head: str,
) -> tuple[str, int, list[dict[str, Any]]]:
    """Lukee vertailun base...head kaikki commitit sivuittain (enintään MAX_COMMITS)."""
    status, total, commits = "", 0, []
    page = 1
    while True:
        params = {"per_page": PAGE_SIZE, "page": page}
        data = api.request("GET", f"/compare/{base}...{head}", params)
        status = str(data.get("status", ""))
        total = int(data.get("total_commits", 0))
        batch = data.get("commits") or []
        commits.extend(batch)
        if total > MAX_COMMITS or len(commits) >= total or not batch:
            return status, total, commits
        page += 1


def check_step_done(api: GitHubApi, run_id: str) -> bool:
    """Valmistuiko epäonnistuneen ajon tarkistus-vaihe (hälytys eikä keskeytys).

    Ennen 0.11.1:tä tarkistus ja hälytys olivat samassa vaiheessa, eikä sen
    epäonnistumisesta voi erottaa hälytystä keskeneräisyydestä. Sellaisessa ajossa ei
    ole tarkistus-vaihetta, joten se ei kelpaa pisteeksi (onnistunut ajo kelpaa).
    """
    data = api.request("GET", f"/actions/runs/{run_id}/jobs", {"per_page": 30})
    for job in (data or {}).get("jobs") or []:
        for step in job.get("steps") or []:
            if step.get("name") == CHECK_STEP and step.get("conclusion") == "success":
                return True
    return False


def previous_checked_head(api: GitHubApi, run_id: str, run_number: int) -> str:
    """Edellisen tarkistuspisteen kärki mainissa, tai tyhjä, jos ajoja ei ole.

    Tarkistuspiste on onnistunut ajo tai epäonnistunut ajo, jonka tarkistus-vaihe
    valmistui (hälytys). Ajo, joka kaatui ennen tarkistusta tai sen aikana, ei kelpaa.
    Vain tätä ajoa aiemmat ajot kelpaavat, jotta vanhan ajon uudelleenajo ei vertaa
    myöhempään kärkeen. Jos ajoja on mutta yksikään ei kelpaa, nostetaan NoCheckpoint.
    """
    params = {
        "branch": MAIN_BRANCH,
        "event": "push",
        "status": "completed",
        "per_page": 30,
    }
    data = api.request("GET", f"/actions/workflows/{WORKFLOW_FILE}/runs", params)
    runs = []
    for run in (data or {}).get("workflow_runs") or []:
        number = int(run.get("run_number") or 0)
        if str(run.get("id")) == run_id:
            continue
        if run_number and number >= run_number:
            continue
        runs.append((number, run))
    runs.sort(key=lambda item: item[0], reverse=True)
    lookups = 0
    for _, run in runs:
        head = str(run.get("head_sha") or "")
        if run.get("conclusion") == "success":
            return head
        if run.get("conclusion") != "failure" or lookups >= MAX_CHECKPOINT_LOOKUPS:
            continue
        lookups += 1
        if check_step_done(api, str(run.get("id"))):
            return head
    if runs:
        raise NoCheckpoint()
    return ""


def choose_base(
    api: GitHubApi,
    before: str,
    after: str,
    run_id: str,
    run_number: int,
    result: Result,
) -> str:
    """Vertailupohja: edellisen vahtiajon kärki, jos välistä puuttuu ajo.

    Push, jonka työnkulku ohitettiin ([skip ci]) tai jonka ajo peruttiin,
    tarkistetaan näin seuraavassa ajossa. Jos edellinen kärki ei ole before-arvon
    esivanhempi, mainin historia muuttui ajojen välissä, ja se on hälytys.
    """
    try:
        previous = previous_checked_head(api, run_id, run_number)
    except NoCheckpoint:
        # Tämä push tarkistetaan kokonaan, ja ajosta tulee seuraava piste (ei lukkoa).
        result.gap.append(
            "Edellisistä vahtiajoista ei löytynyt tarkistuspistettä, joten väli ennen "
            "tämän pushin before-arvoa on todentamatta. Tarkista se käsin."
        )
        return before
    except ApiError as err:
        result.incomplete.append(
            f"Edellisten vahtiajojen luku epäonnistui ({err}), joten väliä edelliseen "
            "tarkistukseen ei todennettu."
        )
        return before
    # Sama kärki voi olla jo tarkistettu (before tai tämän pushin after).
    if previous in ("", before, after) or not SHA_RE.fullmatch(previous):
        return before
    try:
        data = api.request("GET", f"/compare/{previous}...{before}")
        status = str((data or {}).get("status", ""))
    except ApiError as err:
        if err.status != 404:
            msg = f"Edellisen tarkistetun kärjen vertailu epäonnistui: {err}"
            result.incomplete.append(msg)
            return before
        status = "kärki puuttuu"
    if status == "ahead":
        result.notes.append(
            f"Edellinen vahtiajo tarkisti kärjen {previous[:12]}. Väli siitä tämän pushin "
            "before-arvoon jäi ilman vahtiajoa (esim. [skip ci] tai peruttu ajo), joten "
            "se tarkistetaan nyt."
        )
        return previous
    result.reasons.append(
        f"Mainin historia muuttui edellisen vahtiajon jälkeen: sen tarkistama kärki "
        f"{previous[:12]} ei ole tämän pushin before-arvon esivanhempi (tila {status}). "
        "Todennäköisesti pakotettu push ohitti vahdin."
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
    api: GitHubApi,
    commits: list[dict[str, Any]],
    base: str,
    after: str,
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


def check_chain_and_prs(
    api: GitHubApi,
    sleep: Sleep,
    delays: tuple[float, ...],
    commits: list[dict[str, Any]],
    after: str,
    forced: bool,
    result: Result,
) -> None:
    path, reaches_base = first_parent_path(api, commits, result.base, after)
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
            "Mainin ensimmäisen vanhemman ketju ei palaa edelliseen kärkeen, joten main "
            "ei edennyt PR-yhdistämisellä."
        )
    # Uusinta, kun tämän pushin ketju on GitHubin yhdistäminen; muut syyt eivät estä.
    merge_like = not result.outside and reaches_base
    pending = [c for c in commits if c.get("sha")]
    result.checked = len(pending)
    chain = [str(c.get("sha")) for c in path]
    merged_by_sha: dict[str, list[dict[str, Any]]] = {}
    for attempt, delay in enumerate((0, *delays)):
        if attempt and (not merge_like or len(pending) > RETRY_LIMIT):
            break
        if delay:
            sleep(delay)
        missing = []
        for commit in pending:
            path_pulls = f"/commits/{commit['sha']}/pulls"
            prs = api.request("GET", path_pulls, {"per_page": PAGE_SIZE})
            merged = merged_main_prs(prs)
            if not merged:
                missing.append(commit)
            else:
                merged_by_sha[commit["sha"]] = merged
        pending = missing
        if not pending:
            break
    if pending:
        count = len(pending)
        msg = f"{count} commitilla ei ole yhdistettyä PR:ää (kohde {MAIN_BRANCH})."
        result.reasons.append(msg)
        result.commits = [describe(c) for c in pending]
    heads = []
    for sha in chain:
        merged = merged_by_sha.get(sha) or []
        if merged and all(head_sha(pr) == sha for pr in merged):
            numbers = ", ".join(f"#{pr.get('number')}" for pr in merged)
            heads.append(f"{sha[:12]} (PR {numbers})")
    if heads:
        result.reasons.append(
            "Mainin ensimmäisen vanhemman ketjussa on PR:n oma head-commit eikä GitHubin "
            "yhdistämisen luoma commit, eli PR-haara pushattiin suoraan mainiin: "
            + ", ".join(heads)
            + "."
        )


def check_push(
    event: dict[str, Any],
    api: GitHubApi,
    sleep: Sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
    run_id: str = "",
    run_number: int = 0,
    checkout_failed: bool = False,
) -> Result:
    ref = str(event.get("ref") or "")
    if ref != MAIN_REF:
        note = f"Viite {ref or '(puuttuu)'} ei ole {MAIN_REF}; ei tarkistettavaa."
        return Result("skip", notes=[note])
    before = str(event.get("before") or "")
    after = str(event.get("after") or "")
    if event.get("deleted") or is_zero(after):
        note = "Main poistettiin; tarkistettavia committeja ei ole."
        return Result("skip", notes=[note])
    if not SHA_RE.fullmatch(after):
        note = "Tapahtuman after-arvo ei ole commit-tunniste."
        return Result("error", incomplete=[note])
    if event.get("created") or is_zero(before):
        note = (
            "Main luotiin tässä pushissa (esimerkiksi uuden repon ensimmäinen push), "
            "joten vertailupohjaa ei ole. Seuraavat pushit tarkistetaan."
        )
        return Result("skip", notes=[note])
    if not SHA_RE.fullmatch(before):
        note = "Tapahtuman before-arvo ei ole commit-tunniste."
        return Result("error", incomplete=[note])

    result = Result("ok")
    if checkout_failed:
        result.incomplete.append(
            "Vahdin skriptin haku mainin edellisestä kärjestä epäonnistui, joten ajettiin "
            "pushattu versio."
        )
    forced = bool(event.get("forced"))
    if forced:
        note = "Pakotettu push: mainin historia kirjoitettiin uudelleen."
        result.reasons.append(note)
        result.base = before
    else:
        result.base = choose_base(api, before, after, run_id, run_number, result)
    try:
        status, total, commits = compare_commits(api, result.base, after)
    except ApiError as err:
        msg = f"Vertailu epäonnistui, committeja ei tarkistettu: {err}"
        result.incomplete.append(msg)
        return result.settle()
    if status not in ("ahead", "identical") and not forced:
        msg = f"Vertailun tila on {status}: main ei edennyt suoraan eteenpäin."
        result.reasons.append(msg)
    if total > MAX_COMMITS:
        msg = f"Pushissa on {total} committia, yli rajan {MAX_COMMITS}: tarkista käsin."
        result.reasons.append(msg)
        return result.settle()
    try:
        check_chain_and_prs(api, sleep, delays, commits, after, forced, result)
    except ApiError as err:
        result.incomplete.append(f"Ketjun tai PR-kytkennän luku epäonnistui: {err}")
    return result.settle()


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
        sha = code_span(commit["sha"][:12])
        title = code_span(commit["title"])
        author = code_span(commit["author"], 60)
        lines.append(f"- {sha} {title} ({author})")
    if len(commits) > LISTED_COMMITS:
        lines.append(f"- … ja {len(commits) - LISTED_COMMITS} muuta (ks. ajo).")
    return lines


def report_body(event: dict[str, Any], result: Result, repo: str, run_url: str) -> str:
    before = str(event.get("before") or "")[:12]
    after = str(event.get("after") or "")[:12]
    pusher = str((event.get("pusher") or {}).get("name") or "")
    if result.status == "violation":
        intro = (
            "Main-push-vahti havaitsi mainissa muutoksen, joka ei tullut yhdistetyn "
            "pull requestin kautta."
        )
    elif result.status == "gap":
        intro = (
            "Main-push-vahti tarkisti tämän pushin, mutta edellistä tarkistuspistettä ei "
            "löytynyt, joten aiempi väli on todentamatta."
        )
    else:
        intro = (
            "Main-push-vahdin tarkistus ei valmistunut, joten tätä pushia ei ole "
            "todennettu. Tarkista se käsin ja aja vahti uudelleen."
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
    texts = result.reasons + result.incomplete + result.gap + result.notes
    lines += ["", "Syyt:"] + [f"- {text}" for text in texts]
    heading = "Mainin ketjun commitit, joita GitHub ei luonut:"
    lines += commit_lines(heading, result.outside)
    heading = f"Commitit ilman yhdistettyä PR:ää (kohde {MAIN_BRANCH}):"
    lines += commit_lines(heading, result.commits)
    lines += [
        "",
        (
            "Selvitä, kuka pushasi ja miksi. Palauta muutos tarvittaessa revert-PR:llä "
            "ja sulje tämä issue, kun asia on käsitelty. Uudet havainnot lisätään tähän "
            "kommentteina, kunnes issue suljetaan."
        ),
    ]
    return "\n".join(lines) + "\n"


def report_issue(api: GitHubApi, body: str) -> str:
    """Kommentoi vahdin avointa issueta tai avaa uuden. Yksi avoin kerrallaan.

    Vain vahdin (github-actions[bot]) avaamat issuet kelpaavat, jotta kuka tahansa ei
    voi ohjata hälytyksiä omaan samannimiseen issueensa.
    """
    existing: list[int] = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        params = {"state": "open", "per_page": PAGE_SIZE, "page": page}
        issues = api.request("GET", "/issues", params) or []
        for issue in issues:
            if (
                issue.get("title") == ISSUE_TITLE
                and not issue.get("pull_request")
                and (issue.get("user") or {}).get("login") == BOT_LOGIN
            ):
                existing.append(int(issue["number"]))
        if len(issues) < PAGE_SIZE:
            break
    if existing:
        number = min(existing)
        api.request("POST", f"/issues/{number}/comments", body={"body": body})
        return f"Lisätty kommentti issueen #{number}."
    created = api.request("POST", "/issues", body={"title": ISSUE_TITLE, "body": body})
    return f"Avattu issue #{(created or {}).get('number', '?')}."


def summary_text(result: Result, extra: list[str]) -> str:
    heading = {
        "ok": "Kunnossa: jokainen commit tuli yhdistetyn PR:n kautta (kohde main).",
        "skip": "Ei tarkistettavaa.",
        "violation": "HÄLYTYS: suora push mainiin.",
        "gap": "Tämä push on kunnossa, mutta aiempi väli on todentamatta.",
    }.get(result.status, "Tarkistus ei valmistunut.")
    lines = [
        f"## Main-push-vahti: {heading}",
        "",
        f"Tarkistettuja committeja: {result.checked}",
    ]
    texts = result.reasons + result.incomplete + result.gap + result.notes + extra
    lines += [f"- {text}" for text in texts]
    for commit in (result.outside + result.commits)[:LISTED_COMMITS]:
        sha = code_span(commit["sha"][:12])
        lines.append(f"- {sha} {code_span(commit['title'])}")
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
    run_number: int = 0,
    checkout_failed: bool = False,
) -> int:
    result = check_push(event, api, sleep, delays, run_id, run_number, checkout_failed)
    extra: list[str] = []
    on_main = str(event.get("ref") or "") == MAIN_REF
    if result.status in ("violation", "error", "gap") and on_main:
        try:
            extra.append(report_issue(api, report_body(event, result, repo, run_url)))
        except ApiError as err:
            extra.append(f"Issuen avaus tai päivitys epäonnistui: {err}")
    # Jokainen rivi alkaa omalla etuliitteellä, joten commit-otsikko ei voi olla
    # työnkulkukomento.
    prefix = "main-push-vahti:"
    print(f"{prefix} {result.status}, tarkistettuja committeja {result.checked}")
    texts = result.reasons + result.incomplete + result.gap + result.notes + extra
    for text in texts:
        print(f"{prefix} {text}")
    for commit in result.outside:
        sha, title = commit["sha"][:12], commit["title"][:100]
        print(f"{prefix} ei GitHubin luoma {sha} {title}")
    for commit in result.commits:
        sha, title = commit["sha"][:12], commit["title"][:100]
        print(f"{prefix} ilman PR:ää {sha} {title}")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary_text(result, extra))
    return result.exit_code


def finish(code: int, output_path: str | None) -> int:
    """Actionsissa valmis tarkistus (0, 1 tai 3) on 0; tulos menee GITHUB_OUTPUTiin.

    Näin tarkistus-vaihe onnistuu aina, kun tarkistus valmistui, ja seuraava ajo voi
    käyttää sitä tarkistuspisteenä. Hälytys-vaihe kaataa ajon tuloksen perusteella.
    """
    results = {0: "ok", 1: "violation", 3: "gap"}
    if not output_path or code not in results:
        return code
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write(f"tulos={results[code]}\n")
    return 0


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    credential = os.environ.get("GITHUB_TOKEN", "")
    if not (event_path and repo and credential):
        missing = "GITHUB_EVENT_PATH, GITHUB_REPOSITORY tai GITHUB_TOKEN"
        print(f"main-push-vahti: {missing} puuttuu")
        return 2
    # Odottamaton virhe ei saa näyttää hälytykseltä (paluukoodi 1), joten se on 2.
    try:
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_number = int(os.environ.get("GITHUB_RUN_NUMBER") or 0)
        run_url = f"{server}/{repo}/actions/runs/{run_id}"
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        api = GitHubApi(api_url, repo, credential)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        checkout_failed = os.environ.get("VAHTI_EDELLINEN_CHECKOUT") == "failure"
        code = run(
            event,
            api,
            time.sleep,
            repo,
            run_url,
            summary,
            RETRY_DELAYS,
            run_id,
            run_number,
            checkout_failed,
        )
        return finish(code, os.environ.get("GITHUB_OUTPUT"))
    except Exception as err:  # noqa: BLE001
        name = type(err).__name__
        print(f"main-push-vahti: odottamaton virhe {name}; tarkistus ei valmistunut")
        return 2


if __name__ == "__main__":
    sys.exit(main())
