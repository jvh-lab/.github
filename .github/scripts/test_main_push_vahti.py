"""Main-push-vahdin fixture-testit, myös kielteiset tapaukset. Vain vakiokirjasto.

Ajo: python3 -m unittest discover -s .github/scripts -p test_main_push_vahti.py -v
"""

import email.message
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "main_push_vahti.py"
SPEC = importlib.util.spec_from_file_location("main_push_vahti", SCRIPT)
vahti = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vahti
SPEC.loader.exec_module(vahti)
WORKFLOW = HERE.parent / "workflows" / "main-push-vahti.yml"

REPO = "jvh-lab/esimerkki"
RUN_URL = "https://github.com/jvh-lab/esimerkki/actions/runs/1"
BEFORE = "a" * 40
OLD = "b" * 40
PREV = "d" * 40
PR_HEAD = "e" * 40
# 0.11.0:n yhteinen tarkistus- ja hälytysvaihe.
LEGACY = "PR-kytkentä ja GitHubin yhdistäminen jokaiselle commitille"
ZERO = "0" * 40


def sha(n: int) -> str:
    return (f"{n:x}" + "c" * 40)[:40]


def commit(n, parents, github=False, title="", author="Testaaja", verified=None):
    """Commit-fixture. github=True: GitHubin luoma (committer web-flow, varmennettu)."""
    return {
        "sha": sha(n),
        "commit": {
            "message": title or f"commit {n}\n\nrunko",
            "author": {"name": author},
            "verification": {"verified": github if verified is None else verified},
        },
        "committer": {"login": "web-flow" if github else "testaaja"},
        "parents": [{"sha": p} for p in parents],
    }


def pr(number, merged=True, base="main", head=PR_HEAD):
    return {
        "number": number,
        "merged_at": "2026-11-14T20:00:00Z" if merged else None,
        "base": {"ref": base},
        "head": {"sha": head},
    }


def event(**overrides) -> dict:
    data = {
        "ref": "refs/heads/main",
        "before": BEFORE,
        "after": sha(1),
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": "https://github.com/jvh-lab/esimerkki/compare/aaaaaaa...ccccccc",
        "pusher": {"name": "jvhprofileai-prog"},
    }
    data.update(overrides)
    return data


def merge_push(pr_number=5, pr_commits=2):
    """Merge-commit-yhdistäminen: PR:n omat commitit ja GitHubin merge-commit."""
    commits, parent = [], BEFORE
    for n in range(2, 2 + pr_commits):
        commits.append(commit(n, [parent]))
        parent = sha(n)
    title = f"Merge pull request #{pr_number}"
    commits.append(commit(1, [BEFORE, parent], github=True, title=title))
    return commits, {c["sha"]: [pr(pr_number, head=parent)] for c in commits}


def run_entry(run_id, number, head, conclusion="success"):
    return {
        "id": run_id,
        "run_number": number,
        "conclusion": conclusion,
        "head_sha": head,
    }


class FakeApi:
    """Rajapinnan korvike: vastaukset fixtureista, kutsut talteen."""

    def __init__(
        self,
        commits=None,
        pulls=None,
        status="ahead",
        issues=None,
        fail=None,
        runs=None,
        prev_status="ahead",
        full=None,
        jobs=None,
        total=None,
    ):
        self.commits = commits or []
        self.status = status
        # pulls: sha -> lista PR:iä tai funktio, joka palauttaa vastauksen kutsuttaessa.
        self.pulls = pulls or {}
        self.issues = issues or []
        self.fail = fail or {}
        self.runs = runs or []
        self.prev_status = prev_status
        self.full = full or {}
        # jobs: ajon id -> tarkistuspiste-vaiheen conclusion tai {vaihe: conclusion}.
        # Oletus: tarkistuspiste onnistui (uuden version valmis ajo).
        self.jobs = jobs or {}
        self.total = total
        self.calls = []
        self.created = []
        self.comments = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                raise vahti.ApiError(status, path)
        if path.startswith("/actions/workflows/"):
            total = len(self.runs) if self.total is None else self.total
            return {"workflow_runs": self.runs, "total_count": total}
        if path.startswith("/actions/runs/") and path.endswith("/jobs"):
            value = self.jobs.get(int(path.split("/")[3]), "success")
            steps = value if isinstance(value, dict) else {"tarkistuspiste": value}
            listed = [{"name": n, "conclusion": c} for n, c in steps.items()]
            return {"jobs": [{"name": "vahti", "steps": listed}]}
        if path.startswith("/compare/"):
            if params is None:
                return {"status": self.prev_status}
            page, size = params["page"], params["per_page"]
            chunk = self.commits[(page - 1) * size : page * size]
            total = len(self.commits)
            return {"status": self.status, "total_commits": total, "commits": chunk}
        if path.startswith("/commits/") and path.endswith("/pulls"):
            answer = self.pulls.get(path.split("/")[2], [])
            return answer() if callable(answer) else answer
        if path.startswith("/commits/"):
            return self.full[path.split("/")[2]]
        if method == "GET" and path == "/issues":
            page = params["page"]
            return self.issues[(page - 1) * 100 : page * 100]
        if method == "POST" and path == "/issues":
            self.created.append(body)
            return {"number": 99}
        if method == "POST" and path.endswith("/comments"):
            self.comments.append((path, body))
            return {"id": 1}
        raise AssertionError(f"odottamaton kutsu {method} {path}")

    def paths(self, prefix):
        return [c[1] for c in self.calls if c[1].startswith(prefix)]


class Recorder:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def run_check(api, ev=None, delays=(1, 2), run_number=10, checkout_failed=False):
    sleep = Recorder()
    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        code = vahti.run(
            ev or event(),
            api,
            sleep,
            REPO,
            RUN_URL,
            None,
            delays,
            "77",
            run_number,
            checkout_failed,
        )
    return code, sleep, out.getvalue()


def bot_issue(number, title="Suora push mainiin", **extra):
    user = {"login": "github-actions[bot]"}
    return {"number": number, "title": title, "user": user, **extra}


class AcceptedPushes(unittest.TestCase):
    def test_merge_commit_and_pr_commits(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls)
        code, sleep, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.paths("/commits/")), 3)
        self.assertEqual(api.paths("/issues"), [])
        self.assertEqual(sleep.delays, [])
        self.assertIn("main-push-vahti: ok, tarkistettuja committeja 3", out)

    def test_squash_commit(self):
        squash = commit(1, [BEFORE], github=True, title="Ominaisuus (#7)")
        api = FakeApi([squash], {sha(1): [pr(7)]})
        self.assertEqual(run_check(api)[0], 0)

    def test_commit_with_open_and_merged_pr(self):
        merged = commit(1, [BEFORE], github=True)
        api = FakeApi([merged], {sha(1): [pr(3, merged=False), pr(4)]})
        self.assertEqual(run_check(api)[0], 0)

    def test_delayed_association_is_retried(self):
        commits, pulls = merge_push()
        answers = iter([[], [pr(5)]])
        pulls[sha(1)] = lambda: next(answers)
        api = FakeApi(commits, pulls)
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(sleep.delays, [1])
        calls = api.paths("/commits/")
        self.assertEqual(calls.count(f"/commits/{sha(2)}/pulls"), 1)

    def test_compare_is_paginated(self):
        commits, pulls = merge_push(pr_commits=149)
        api = FakeApi(commits, pulls)
        code, _, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.paths("/compare/")), 2)
        self.assertIn("tarkistettuja committeja 150", out)

    def test_missing_verification_is_fetched(self):
        merge = commit(1, [BEFORE], github=True)
        info = {k: v for k, v in merge["commit"].items() if k != "verification"}
        light = {**merge, "commit": info}
        api = FakeApi([light], {sha(1): [pr(9)]}, full={sha(1): merge})
        self.assertEqual(run_check(api)[0], 0)
        self.assertIn(f"/commits/{sha(1)}", api.paths("/commits/"))

    def test_previous_run_at_before_keeps_base(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls, runs=[run_entry(5, 5, BEFORE)])
        self.assertEqual(run_check(api)[0], 0)
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])

    def test_rerun_of_older_run_ignores_newer_runs(self):
        # Vanhan ajon uudelleenajo: uudemmat ajot (run_number >= oma) eivät ole pohja.
        commits, pulls = merge_push()
        runs = [run_entry(9, 9, PREV), run_entry(4, 4, BEFORE)]
        api = FakeApi(commits, pulls, runs=runs, prev_status="behind")
        code, _, _ = run_check(api, run_number=5)
        self.assertEqual(code, 0)
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])


class Skipped(unittest.TestCase):
    def assert_skip(self, ev):
        api = FakeApi()
        code, _, out = run_check(api, ev)
        self.assertEqual(code, 0)
        self.assertEqual(api.calls, [])
        self.assertIn("main-push-vahti: skip", out)

    def test_tag_push(self):
        self.assert_skip(event(ref="refs/tags/v1.0.0"))

    def test_other_branch(self):
        self.assert_skip(event(ref="refs/heads/feat/x"))

    def test_branch_created_before_is_zero(self):
        self.assert_skip(event(before=ZERO, created=True))

    def test_before_zero_without_created_flag(self):
        self.assert_skip(event(before=ZERO))

    def test_branch_deleted(self):
        self.assert_skip(event(after=ZERO, deleted=True))


class DirectPushes(unittest.TestCase):
    def test_direct_push_opens_issue(self):
        direct = commit(1, [BEFORE], title="korjaus suoraan mainiin")
        api = FakeApi([direct], {sha(1): []})
        code, sleep, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.created[0]["title"], "Suora push mainiin")
        body = api.created[0]["body"]
        self.assertIn(sha(1)[:12], body)
        self.assertIn("korjaus suoraan mainiin", body)
        self.assertIn(RUN_URL, body)
        self.assertEqual(sleep.delays, [], "suoraa pushia ei jäädä odottamaan")
        self.assertIn("Avattu issue #99", out)

    def test_pr_branch_pushed_to_main_is_indirect_merge(self):
        # GitHub merkitsee PR:n yhdistetyksi, kun sen commitit pushataan mainiin.
        commits = [commit(2, [BEFORE]), commit(1, [sha(2)])]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn("ei ole GitHubin luoma yhdistäminen", body)
        self.assertIn(sha(1)[:12], body)
        self.assertIn(sha(2)[:12], body)

    def test_update_branch_commit_pushed_to_main(self):
        # Update branch -painikkeen merge-commit on GitHubin luoma, mutta sen
        # ensimmäinen vanhempi on PR-haara eikä main.
        commits = [commit(2, [OLD]), commit(1, [sha(2), BEFORE], github=True)]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn("ei palaa edelliseen kärkeen", body)
        self.assertIn(sha(2)[:12], body)

    def test_local_merge_pushed_to_main(self):
        local = commit(1, [BEFORE, sha(2)], title="Merge branch 'feat/x'")
        commits = [commit(2, [BEFORE]), local]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        self.assertEqual(run_check(api)[0], 1)

    def test_web_editor_pr_branch_pushed_to_main(self):
        # Kaikki PR:n commitit ovat GitHubin allekirjoittamia (verkkoeditori), ja GitHub
        # merkitsee PR:n epäsuorasti yhdistetyksi. Kärki on silti PR:n oma head.
        commits = [commit(2, [BEFORE], github=True), commit(1, [sha(2)], github=True)]
        pulls = {c["sha"]: [pr(42, head=sha(1))] for c in commits}
        api = FakeApi(commits, pulls)
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("oma head-commit", api.created[0]["body"])

    def web_editor_head_then_merge(self):
        # PR #42:n head A tehtiin verkkoeditorilla ja pushattiin suoraan mainiin.
        # Sen jälkeen PR #7 yhdistettiin GitHubissa (merge M, ensimmäinen vanhempi A).
        a = commit(20, [PREV], github=True, title="muutos [skip ci]")
        c_pr = commit(2, [sha(20)])
        m = commit(1, [sha(20), sha(2)], github=True, title="Merge pull request #7")
        pulls = {
            sha(20): [pr(42, head=sha(20))],
            sha(2): [pr(7, head=sha(2))],
            sha(1): [pr(7, head=sha(2))],
        }
        return [a, c_pr, m], pulls

    def test_web_editor_pr_head_skipped_by_skip_ci_is_found(self):
        commits, pulls = self.web_editor_head_then_merge()
        api = FakeApi(commits, pulls, runs=[run_entry(7, 7, PREV)])
        code, _, _ = run_check(api, event(before=sha(20)))
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn("oma head-commit", body)
        self.assertIn(f"{sha(20)[:12]} (PR #42)", body)

    def test_web_editor_pr_head_after_run_failed_before_check(self):
        commits, pulls = self.web_editor_head_then_merge()
        runs = [run_entry(8, 8, sha(20), "failure"), run_entry(7, 7, PREV)]
        api = FakeApi(commits, pulls, runs=runs, jobs={8: "skipped"})
        code, _, _ = run_check(api, event(before=sha(20)))
        self.assertEqual(code, 1)
        self.assertIn(f"{sha(20)[:12]} (PR #42)", api.created[0]["body"])

    def test_tip_head_check_accepts_any_real_merge(self):
        commits, pulls = merge_push()
        pulls[sha(1)] = [pr(9, head=sha(1)), pr(5, head=sha(3))]
        self.assertEqual(run_check(FakeApi(commits, pulls))[0], 0)

    def test_forged_committer_without_signature(self):
        forged = commit(1, [BEFORE], github=True, verified=False)
        api = FakeApi([forged], {sha(1): [pr(5)]})
        self.assertEqual(run_check(api)[0], 1)

    def test_rebase_merge_alarms_because_github_does_not_sign(self):
        # GitHubin ohje: rebase-yhdistämisen committeja ei allekirjoiteta, joten
        # vahti ei tue rebase-yhdistämistä (jvh-lab käyttää merge-yhdistämistä).
        commits = [
            commit(3, [BEFORE], github=True, verified=False),
            commit(2, [sha(3)], github=True, verified=False),
            commit(1, [sha(2)], github=True, verified=False),
        ]
        api = FakeApi(commits, {c["sha"]: [pr(8)] for c in commits})
        self.assertEqual(run_check(api)[0], 1)

    def test_web_editor_commit_without_pr(self):
        api = FakeApi([commit(1, [BEFORE], github=True)], {sha(1): []})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("ei ole yhdistettyä PR:ää", api.created[0]["body"])

    def test_skipped_push_is_checked_on_next_run(self):
        # [skip ci] -commit X ei käynnistänyt vahtia; seuraava yhdistäminen tarkistaa sen.
        x = commit(20, [PREV], title="korjaus [skip ci]")
        commits, pulls = merge_push()
        commits[0]["parents"] = [{"sha": sha(20)}]
        commits[-1]["parents"][0] = {"sha": sha(20)}
        runs = [
            run_entry(77, 9, sha(1), conclusion=None),
            run_entry(6, 6, PREV),
            run_entry(4, 4, OLD),
        ]
        api = FakeApi([x, *commits], {**pulls, sha(20): []}, runs=runs)
        code, _, _ = run_check(api, event(before=sha(20)))
        self.assertEqual(code, 1)
        self.assertIn(f"/compare/{PREV}...{sha(1)}", api.paths("/compare/"))
        body = api.created[0]["body"]
        self.assertIn("korjaus [skip ci]", body)
        self.assertIn("Vertailupohja", body)

    def test_run_failed_before_check_is_not_checkpoint(self):
        # Ketju: ohitettu [skip ci] -push D, seuraavan yhdistämisen B ajo kaatui ennen
        # tarkistusta, ja nyt yhdistetään C. D tarkistetaan, koska B ei ole piste.
        d = commit(20, [PREV], title="suora [skip ci]")
        b_pr = commit(22, [sha(20)])
        b = commit(21, [sha(20), sha(22)], github=True, title="Merge pull request #6")
        c_pr = commit(2, [sha(21)])
        c = commit(1, [sha(21), sha(2)], github=True, title="Merge pull request #7")
        pulls = {
            sha(22): [pr(6, head=sha(22))],
            sha(21): [pr(6, head=sha(22))],
            sha(2): [pr(7, head=sha(2))],
            sha(1): [pr(7, head=sha(2))],
        }
        runs = [run_entry(8, 8, sha(21), "failure"), run_entry(7, 7, PREV)]
        api = FakeApi([d, b_pr, b, c_pr, c], pulls, runs=runs, jobs={8: "failure"})
        code, _, _ = run_check(api, event(before=sha(21)))
        self.assertEqual(code, 1)
        self.assertIn(f"/compare/{PREV}...{sha(1)}", api.paths("/compare/"))
        self.assertIn("suora [skip ci]", api.created[0]["body"])

    def test_failure_run_with_completed_check_is_checkpoint(self):
        commits, pulls = merge_push()
        runs = [run_entry(8, 8, PREV, "failure")]
        api = FakeApi(commits, pulls, runs=runs, jobs={8: "success"})
        run_check(api)
        self.assertIn(f"/compare/{PREV}...{sha(1)}", api.paths("/compare/"))

    def test_no_checkpoint_checks_this_push_and_reports_gap(self):
        commits, pulls = merge_push()
        runs = [run_entry(8, 8, PREV, "failure"), run_entry(7, 7, OLD, "cancelled")]
        api = FakeApi(commits, pulls, runs=runs, jobs={8: "failure"})
        code, _, _ = run_check(api)
        self.assertEqual(code, 3)
        self.assertIn(f"/compare/{BEFORE}...{sha(1)}", api.paths("/compare/"))
        self.assertEqual(len(api.paths("/commits/")), 3, "tämä push tarkistetaan")
        self.assertIn("ei löytynyt tarkistuspistettä", api.created[0]["body"])

    def test_gap_run_becomes_checkpoint_so_there_is_no_lock(self):
        # Ajo #9 raportoi aukon (tulos=gap, tarkistus-vaihe onnistui), joten ajo #10
        # käyttää sitä pisteenä eikä jää pysyvään keskeneräiseen tilaan.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.txt"
            self.assertEqual(vahti.finish(3, str(output)), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "tulos=gap\n")
        commits, pulls = merge_push()
        runs = [run_entry(9, 9, BEFORE, "failure"), run_entry(8, 8, PREV, "failure")]
        api = FakeApi(commits, pulls, runs=runs, jobs={9: "success", 8: "failure"})
        code, _, _ = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])

    def test_transition_after_incomplete_legacy_run_reports_gap(self):
        # A onnistunut 0.11.0-ajo, D suora [skip ci] -push, B 0.11.0-merge, jonka ajo
        # kaatui 503:een (koodi 2), C päivitysmerge: uusi työnkulku ajaa 0.11.0-skriptin
        # (tarkistus onnistuu, tarkistuspiste ohitetaan). E on uuden version ajo.
        legacy = {LEGACY: "failure"}
        update = {"tarkistus": "success", "tarkistuspiste": "skipped"}
        old_ok = {LEGACY: "success"}
        runs = [
            run_entry(7, 7, BEFORE),
            run_entry(6, 6, sha(21), "failure"),
            run_entry(5, 5, PREV),
        ]
        commits, pulls = merge_push()
        jobs = {7: update, 6: legacy, 5: old_ok}
        api = FakeApi(commits, pulls, runs=runs, jobs=jobs)
        code, _, _ = run_check(api)
        self.assertEqual(code, 3, "D:tä ei voi todentaa, joten gap eikä hiljainen 0")
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])
        self.assertIn("ei löytynyt tarkistuspistettä", api.created[0]["body"])

    def test_transition_with_only_successful_legacy_runs_uses_latest(self):
        # Kaikki 0.11.0-ajot onnistuivat ja lista on täydellinen: ketju on yhtenäinen.
        update = {"tarkistus": "success", "tarkistuspiste": "skipped"}
        old_ok = {LEGACY: "success"}
        runs = [run_entry(7, 7, BEFORE), run_entry(6, 6, PREV), run_entry(5, 5, OLD)]
        commits, pulls = merge_push()
        jobs = {7: update, 6: old_ok, 5: old_ok}
        api = FakeApi(commits, pulls, runs=runs, jobs=jobs)
        code, _, _ = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])

    def test_transition_with_incomplete_run_list_reports_gap(self):
        old_ok = {LEGACY: "success"}
        commits, pulls = merge_push()
        runs = [run_entry(6, 6, BEFORE)]
        api = FakeApi(commits, pulls, runs=runs, jobs={6: old_ok}, total=31)
        self.assertEqual(run_check(api)[0], 3)

    def test_successful_run_without_checkpoint_step_needs_transition_rule(self):
        # Uuden työnkulun onnistunut ajo, jossa tarkistuspiste ohitettiin, ei ole piste
        # sellaisenaan; aiempi epäonnistunut ajo estää siirtymäsäännön.
        update = {"tarkistus": "success", "tarkistuspiste": "skipped"}
        legacy = {LEGACY: "failure"}
        commits, pulls = merge_push()
        runs = [run_entry(8, 8, BEFORE), run_entry(7, 7, PREV, "failure")]
        api = FakeApi(commits, pulls, runs=runs, jobs={8: update, 7: legacy})
        self.assertEqual(run_check(api)[0], 3)

    def test_history_rewritten_between_runs_alarms(self):
        # Pakotettu push, jonka vahtiajo ohitettiin tai peruttiin.
        commits, pulls = merge_push()
        runs = [run_entry(6, 6, PREV, conclusion="failure")]
        api = FakeApi(commits, pulls, runs=runs, prev_status="diverged")
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn(f"/compare/{BEFORE}...{sha(1)}", api.paths("/compare/"))
        self.assertIn("historia muuttui", api.created[0]["body"])

    def test_history_alarm_does_not_list_delayed_merge_as_direct(self):
        commits, pulls = merge_push()
        answers = iter([[], [pr(5)]])
        pulls[sha(1)] = lambda: next(answers)
        runs = [run_entry(6, 6, PREV)]
        api = FakeApi(commits, pulls, runs=runs, prev_status="diverged")
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(sleep.delays, [1])
        body = api.created[0]["body"]
        self.assertIn("historia muuttui", body)
        self.assertNotIn("ilman yhdistettyä PR:ää", body)

    def test_previous_run_already_checked_this_head(self):
        commits, pulls = merge_push()
        runs = [run_entry(9, 9, sha(1))]
        api = FakeApi(commits, pulls, runs=runs, prev_status="behind")
        self.assertEqual(run_check(api)[0], 0)
        self.assertEqual(api.paths("/compare/"), [f"/compare/{BEFORE}...{sha(1)}"])

    def test_previous_head_missing_alarms(self):
        commits, pulls = merge_push()
        runs = [run_entry(6, 6, PREV)]
        fail = {f"/compare/{PREV}": 404}
        api = FakeApi(commits, pulls, runs=runs, fail=fail)
        self.assertEqual(run_check(api)[0], 1)
        self.assertIn("kärki puuttuu", api.created[0]["body"])

    def test_open_issue_is_commented_not_duplicated(self):
        issues = [
            bot_issue(4, pull_request={"url": "x"}),
            bot_issue(12),
            bot_issue(6),
            bot_issue(2, title="Muu asia"),
        ]
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []}, issues=issues)
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.created, [])
        self.assertEqual([c[0] for c in api.comments], ["/issues/6/comments"])
        self.assertIn("issueen #6", out)

    def test_decoy_issue_by_other_user_is_ignored(self):
        user = {"login": "joku"}
        decoy = {"number": 3, "title": "Suora push mainiin", "user": user}
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []}, issues=[decoy])
        self.assertEqual(run_check(api)[0], 1)
        self.assertEqual(api.comments, [])
        self.assertEqual(len(api.created), 1)

    def test_issue_search_is_paginated(self):
        issues = [bot_issue(n, title=f"asia {n}") for n in range(1, 101)]
        issues.append(bot_issue(150))
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []}, issues=issues)
        self.assertEqual(run_check(api)[0], 1)
        self.assertEqual([c[0] for c in api.comments], ["/issues/150/comments"])

    def test_only_open_pr_is_not_enough(self):
        merged = commit(1, [BEFORE], github=True)
        api = FakeApi([merged], {sha(1): [pr(3, merged=False)]})
        self.assertEqual(run_check(api)[0], 1)

    def test_pr_merged_into_other_branch_is_not_enough(self):
        merged = commit(1, [BEFORE], github=True)
        api = FakeApi([merged], {sha(1): [pr(3, base="develop")]})
        self.assertEqual(run_check(api)[0], 1)

    def test_one_direct_commit_among_pr_commits(self):
        commits, pulls = merge_push()
        pulls[sha(2)] = []
        api = FakeApi(commits, pulls)
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn(sha(2)[:12], body)
        self.assertNotIn(sha(3)[:12], body)

    def test_forced_push_is_violation_even_with_pr_commits(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls, status="diverged")
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 1)
        self.assertIn("Pakotettu push", api.created[0]["body"])
        self.assertEqual(api.paths("/actions/"), [], "pakotetussa pohja on before")

    def test_forced_reset_to_older_commit(self):
        api = FakeApi([], status="behind")
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)

    def test_non_fast_forward_without_forced_flag(self):
        api = FakeApi([], status="behind")
        self.assertEqual(run_check(api)[0], 1)

    def test_too_many_commits_fails_closed(self):
        commits = [commit(n, [BEFORE]) for n in range(1, vahti.MAX_COMMITS + 2)]
        api = FakeApi(commits)
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.paths("/commits/"), [])
        self.assertIn("yli rajan", api.created[0]["body"])

    def test_large_merge_with_missing_association_is_not_retried(self):
        commits, pulls = merge_push(pr_commits=vahti.RETRY_LIMIT + 1)
        for c in commits:
            pulls[c["sha"]] = []
        api = FakeApi(commits, pulls)
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(sleep.delays, [], "yli rajan ei uusita")

    def test_issue_failure_still_fails_the_run(self):
        fail = {"/issues": 410}
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []}, fail=fail)
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("Issuen avaus tai päivitys epäonnistui", out)

    def test_untrusted_text_cannot_mention_or_inject(self):
        title = "::error::huijaus @juha `x` " + "a" * 200
        direct = commit(1, [BEFORE], title=title, author="@toinen")
        api = FakeApi([direct], {sha(1): []})
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        outside_code = re.sub(r"`[^`]*`", "", body)
        self.assertNotIn("@", outside_code)
        self.assertNotIn("::error::", outside_code)
        lines = out.splitlines()
        self.assertTrue(all(line.startswith("main-push-vahti: ") for line in lines))


class Errors(unittest.TestCase):
    def test_compare_failure_is_error_and_reported(self):
        api = FakeApi(fail={"/compare/": 404})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2)
        self.assertEqual(len(api.created), 1)
        self.assertIn("tarkistus ei valmistunut", api.created[0]["body"])

    def test_compare_failure_on_forced_push_still_alarms(self):
        api = FakeApi(fail={"/compare/": 404})
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 2, "hälytys, mutta keskeneräinen")
        self.assertEqual(len(api.created), 1)
        self.assertIn("Pakotettu push", api.created[0]["body"])

    def test_association_failure_is_error_and_reported(self):
        commits, _ = merge_push()
        api = FakeApi(commits, fail={"/commits/": 500})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2)
        self.assertEqual(len(api.created), 1)

    def test_association_failure_after_found_reason_alarms(self):
        api = FakeApi([commit(1, [BEFORE])], fail={"/commits/": 500})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2, "hälytys, mutta keskeneräinen ei ole piste")
        self.assertIn("havaitsi mainissa muutoksen", api.created[0]["body"])
        self.assertEqual(vahti.finish(code, None), 2)

    def test_violation_with_incomplete_history_is_not_checkpoint(self):
        # Codexin ketju: D suora [skip ci] -push, sitten suora push B, kun ajohistorian
        # rajapinta palauttaa 503. B:n ajo hälyttää, mutta ei kelpaa pisteeksi, joten
        # seuraava yhdistäminen C tarkistaa D:n.
        b = commit(21, [sha(20)], title="suora push B")
        api = FakeApi([b], {sha(21): []}, fail={"/actions/": 503})
        code, _, _ = run_check(api, event(before=sha(20), after=sha(21)))
        self.assertEqual(code, 2)
        self.assertEqual(len(api.created), 1)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.txt"
            self.assertEqual(vahti.finish(code, str(output)), 2)
            self.assertFalse(output.exists(), "keskeneräinen ei kirjoita tulosta")
        d = commit(20, [PREV], title="suora D [skip ci]")
        c_pr = commit(2, [sha(21)])
        c = commit(1, [sha(21), sha(2)], github=True, title="Merge pull request #7")
        pulls = {sha(2): [pr(7, head=sha(2))], sha(1): [pr(7, head=sha(2))]}
        runs = [run_entry(8, 8, sha(21), "failure"), run_entry(7, 7, PREV)]
        api = FakeApi([d, b, c_pr, c], pulls, runs=runs, jobs={8: "failure"})
        code, _, _ = run_check(api, event(before=sha(21)))
        self.assertEqual(code, 1)
        self.assertIn(f"/compare/{PREV}...{sha(1)}", api.paths("/compare/"))
        self.assertIn("suora D [skip ci]", api.created[0]["body"])

    def test_runs_api_failure_is_reported_but_push_is_checked(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls, fail={"/actions/": 403})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2)
        self.assertEqual(len(api.paths("/commits/")), 3)
        body = api.created[0]["body"]
        self.assertIn("Edellisten vahtiajojen luku epäonnistui", body)

    def test_previous_compare_validation_error_is_incomplete(self):
        commits, pulls = merge_push()
        runs = [run_entry(6, 6, PREV)]
        fail = {f"/compare/{PREV}": 422}
        api = FakeApi(commits, pulls, runs=runs, fail=fail)
        self.assertEqual(run_check(api)[0], 2)
        self.assertIn("vertailu epäonnistui", api.created[0]["body"])

    def test_previous_checkout_failure_is_reported(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls)
        code, _, _ = run_check(api, checkout_failed=True)
        self.assertEqual(code, 2)
        self.assertIn("pushattu versio", api.created[0]["body"])

    def test_invalid_sha_is_error(self):
        api = FakeApi()
        self.assertEqual(run_check(api, event(after="main"))[0], 2)
        self.assertEqual(run_check(api, event(after=sha(1) + "\n"))[0], 2)
        self.assertEqual(run_check(api, event(before="x" * 40))[0], 2)
        self.assertEqual(api.paths("/compare/"), [])

    def test_main_requires_environment(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(vahti.main(), 2)

    def test_unexpected_exception_is_not_an_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(event()), encoding="utf-8")
            env = {
                "GITHUB_EVENT_PATH": str(path),
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_TOKEN": "x",
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(vahti, "run", side_effect=KeyError("number")),
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                self.assertEqual(vahti.main(), 2)
        self.assertIn("odottamaton virhe KeyError", out.getvalue())

    def test_finish_writes_result_for_completed_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.txt"
            self.assertEqual(vahti.finish(1, str(path)), 0)
            self.assertEqual(vahti.finish(0, str(path)), 0)
            self.assertEqual(vahti.finish(2, str(path)), 2)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text, "tulos=violation\ntulos=ok\n")
        self.assertEqual(vahti.finish(1, None), 1)
        self.assertEqual(vahti.finish(3, None), 3)

    def test_summary_is_written(self):
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                code = vahti.run(event(), api, Recorder(), REPO, RUN_URL, str(path), ())
            text = path.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn("HÄLYTYS: suora push mainiin", text)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(req, code, headers=None):
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(req.full_url, code, "virhe", msg, None)


class RealClient(unittest.TestCase):
    def client(self, sleep):
        return vahti.GitHubApi("https://api.github.com/", REPO, "kokeilu", sleep)

    def test_request_headers_and_url(self):
        seen = []

        def fake_urlopen(req, timeout):
            seen.append((req, timeout))
            return FakeResponse(json.dumps([{"number": 1}]).encode())

        path = f"/commits/{sha(1)}/pulls"
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = self.client(Recorder()).request("GET", path, {"per_page": 100})
        self.assertEqual(data, [{"number": 1}])
        req, timeout = seen[0]
        url = f"https://api.github.com/repos/{REPO}{path}?per_page=100"
        self.assertEqual(req.full_url, url)
        self.assertEqual(req.get_header("X-github-api-version"), vahti.API_VERSION)
        self.assertEqual(req.get_header("Authorization"), "Bearer kokeilu")
        self.assertEqual(timeout, 30)

    def test_client_error_is_not_retried(self):
        sleep = Recorder()

        def fail(req, timeout):
            raise http_error(req, 404)

        with (
            mock.patch("urllib.request.urlopen", fail),
            self.assertRaises(vahti.ApiError) as ctx,
        ):
            self.client(sleep).request("GET", "/compare/a...b")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(sleep.delays, [])

    def test_network_error_on_read_is_retried_then_raised(self):
        sleep = Recorder()

        def fail(req, timeout):
            raise urllib.error.URLError("katkos")

        with (
            mock.patch("urllib.request.urlopen", fail),
            self.assertRaises(vahti.ApiError) as ctx,
        ):
            self.client(sleep).request("GET", "/issues")
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(sleep.delays, [2, 5])

    def test_write_is_not_retried_after_network_error(self):
        sleep = Recorder()
        calls = []

        def fail(req, timeout):
            calls.append(req)
            raise urllib.error.URLError("katkos")

        with (
            mock.patch("urllib.request.urlopen", fail),
            self.assertRaises(vahti.ApiError),
        ):
            self.client(sleep).request("POST", "/issues", body={"title": "x"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_rate_limit_waits_retry_after(self):
        sleep = Recorder()
        answers = iter(["limit", "ok"])

        def flaky(req, timeout):
            if next(answers) == "limit":
                raise http_error(req, 429, {"Retry-After": "3"})
            return FakeResponse(b"[]")

        with mock.patch("urllib.request.urlopen", flaky):
            self.assertEqual(self.client(sleep).request("GET", "/issues"), [])
        self.assertEqual(sleep.delays, [3.0])

    def test_forbidden_without_rate_headers_is_not_retried(self):
        sleep = Recorder()

        def fail(req, timeout):
            raise http_error(req, 403, {"X-RateLimit-Remaining": "4999"})

        with (
            mock.patch("urllib.request.urlopen", fail),
            self.assertRaises(vahti.ApiError),
        ):
            self.client(sleep).request("GET", "/issues")
        self.assertEqual(sleep.delays, [])

    def test_non_json_body_is_api_error(self):
        def html(req, timeout):
            return FakeResponse(b"<html>")

        with (
            mock.patch("urllib.request.urlopen", html),
            self.assertRaises(vahti.ApiError) as ctx,
        ):
            self.client(Recorder()).request("GET", "/issues")
        self.assertEqual(ctx.exception.status, 0)


class WorkflowFile(unittest.TestCase):
    """Työnkulun rajat tekstitarkistuksina (PyYAML ei ole vakiokirjastossa)."""

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_triggers_and_runner(self):
        self.assertRegex(self.text, r"(?m)^  push:\n    branches: \[main\]\n")
        self.assertNotRegex(self.text, r"(?m)^\s+tags:")
        self.assertNotIn("self-hosted", self.text)
        runners = re.findall(r"runs-on: (\S+)", self.text)
        self.assertEqual(runners, ["ubuntu-latest"] * 2)

    def test_minimal_permissions(self):
        self.assertRegex(self.text, r"(?m)^permissions: \{\}$")
        grants = re.findall(r"(?m)^      ([a-z-]+): (read|write)$", self.text)
        expected = [
            ("contents", "read"),
            ("pull-requests", "read"),
            ("issues", "write"),
            ("actions", "read"),
            ("contents", "read"),
        ]
        self.assertEqual(grants, expected)

    def test_script_comes_from_previous_main_with_fallback(self):
        self.assertIn("ref: ${{ github.event.before }}", self.text)
        fallback = "if: hashFiles('.github/scripts/main_push_vahti.py') == ''"
        self.assertIn(fallback, self.text)
        outcome = "VAHTI_EDELLINEN_CHECKOUT: ${{ steps.edellinen.outcome }}"
        self.assertIn(outcome, self.text)

    def test_check_step_is_checkpoint_and_alarm_is_separate(self):
        step = r"(?m)^      - name: tarkistus\n        id: tarkistus\n"
        self.assertRegex(self.text, step)
        point = "      - name: tarkistuspiste\n"
        condition = "        if: steps.tarkistus.outputs.tulos != ''\n"
        self.assertIn(point + condition, self.text)
        alarm = (
            "if: steps.tarkistus.outputs.tulos == 'violation' "
            "|| steps.tarkistus.outputs.tulos == 'gap'"
        )
        self.assertIn(alarm, self.text)
        self.assertEqual(vahti.CHECK_STEP, "tarkistuspiste")

    def test_actions_are_pinned_to_sha(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertEqual(len(uses), 3)
        for ref in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        self.assertNotIn("secrets.", self.text)


if __name__ == "__main__":
    unittest.main()
