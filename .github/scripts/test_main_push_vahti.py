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
SPEC = importlib.util.spec_from_file_location("main_push_vahti", HERE / "main_push_vahti.py")
vahti = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vahti
SPEC.loader.exec_module(vahti)
WORKFLOW = HERE.parent / "workflows" / "main-push-vahti.yml"

REPO = "jvh-lab/esimerkki"
RUN_URL = "https://github.com/jvh-lab/esimerkki/actions/runs/1"
BEFORE = "a" * 40
OLD = "b" * 40
PREV = "d" * 40
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


def pr(number: int, merged: bool = True, base: str = "main") -> dict:
    return {
        "number": number,
        "merged_at": "2026-11-14T20:00:00Z" if merged else None,
        "base": {"ref": base},
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
    merge = commit(1, [BEFORE, parent], github=True, title=f"Merge pull request #{pr_number}")
    commits.append(merge)
    return commits, {c["sha"]: [pr(pr_number)] for c in commits}


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
        self.calls = []
        self.created = []
        self.comments = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                raise vahti.ApiError(status, path)
        if path.startswith("/actions/workflows/"):
            return {"workflow_runs": self.runs}
        if path.startswith("/compare/"):
            if params is None:
                return {"status": self.prev_status}
            page, size = params["page"], params["per_page"]
            chunk = self.commits[(page - 1) * size : page * size]
            return {"status": self.status, "total_commits": len(self.commits), "commits": chunk}
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


def run_check(api, ev=None, delays=(1, 2), run_id="77"):
    sleep = Recorder()
    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        code = vahti.run(ev or event(), api, sleep, REPO, RUN_URL, None, delays, run_id)
    return code, sleep, out.getvalue()


def bot_issue(number, title="Suora push mainiin", **extra):
    return {"number": number, "title": title, "user": {"login": "github-actions[bot]"}, **extra}


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
        api = FakeApi(
            [commit(1, [BEFORE], github=True, title="Ominaisuus (#7)")], {sha(1): [pr(7)]}
        )
        self.assertEqual(run_check(api)[0], 0)

    def test_rebase_merged_commits(self):
        commits = [
            commit(3, [BEFORE], github=True),
            commit(2, [sha(3)], github=True),
            commit(1, [sha(2)], github=True),
        ]
        api = FakeApi(commits, {c["sha"]: [pr(8)] for c in commits})
        self.assertEqual(run_check(api)[0], 0)

    def test_commit_with_open_and_merged_pr(self):
        api = FakeApi([commit(1, [BEFORE], github=True)], {sha(1): [pr(3, merged=False), pr(4)]})
        self.assertEqual(run_check(api)[0], 0)

    def test_delayed_association_is_retried(self):
        commits, pulls = merge_push()
        answers = iter([[], [pr(5)]])
        pulls[sha(1)] = lambda: next(answers)
        api = FakeApi(commits, pulls)
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(sleep.delays, [1])
        self.assertEqual(api.paths("/commits/").count(f"/commits/{sha(2)}/pulls"), 1)

    def test_compare_is_paginated(self):
        commits, pulls = merge_push(pr_commits=149)
        api = FakeApi(commits, pulls)
        code, _, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.paths("/compare/")), 2)
        self.assertIn("tarkistettuja committeja 150", out)

    def test_missing_verification_is_fetched(self):
        merge = commit(1, [BEFORE], github=True)
        light = {
            **merge,
            "commit": {k: v for k, v in merge["commit"].items() if k != "verification"},
        }
        api = FakeApi([light], {sha(1): [pr(9)]}, full={sha(1): merge})
        self.assertEqual(run_check(api)[0], 0)
        self.assertIn(f"/commits/{sha(1)}", api.paths("/commits/"))

    def test_previous_run_at_before_keeps_base(self):
        commits, pulls = merge_push()
        runs = [{"id": 5, "run_number": 5, "conclusion": "success", "head_sha": BEFORE}]
        api = FakeApi(commits, pulls, runs=runs)
        self.assertEqual(run_check(api)[0], 0)
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
        api = FakeApi([commit(1, [BEFORE], title="korjaus suoraan mainiin")], {sha(1): []})
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
        # GitHub merkitsee PR:n yhdistetyksi, kun sen commitit pushataan suoraan mainiin.
        commits = [commit(2, [BEFORE]), commit(1, [sha(2)])]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn("ei ole GitHubin luoma yhdistäminen", body)
        self.assertIn(sha(1)[:12], body)
        self.assertIn(sha(2)[:12], body)

    def test_update_branch_commit_pushed_to_main(self):
        # Update branch -painikkeen merge-commit on GitHubin luoma, mutta ensimmäinen vanhempi
        # on PR-haara eikä main.
        commits = [commit(2, [OLD]), commit(1, [sha(2), BEFORE], github=True)]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn("ei palaa edelliseen kärkeen", body)
        self.assertIn(sha(2)[:12], body)

    def test_local_merge_pushed_to_main(self):
        commits = [commit(2, [BEFORE]), commit(1, [BEFORE, sha(2)], title="Merge branch 'feat/x'")]
        api = FakeApi(commits, {c["sha"]: [pr(42)] for c in commits})
        self.assertEqual(run_check(api)[0], 1)

    def test_forged_committer_without_signature(self):
        forged = commit(1, [BEFORE], github=True, verified=False)
        api = FakeApi([forged], {sha(1): [pr(5)]})
        self.assertEqual(run_check(api)[0], 1)

    def test_web_editor_commit_without_pr(self):
        api = FakeApi([commit(1, [BEFORE], github=True)], {sha(1): []})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("ei ole yhdistettyä PR:ää", api.created[0]["body"])

    def test_skipped_push_is_checked_on_next_run(self):
        # [skip ci] -commit X ei käynnistänyt vahtia; seuraava PR-yhdistäminen tarkistaa sen.
        x = commit(20, [PREV], title="korjaus [skip ci]")
        commits, pulls = merge_push()
        commits[0]["parents"] = [{"sha": sha(20)}]
        commits[-1]["parents"][0] = {"sha": sha(20)}
        runs = [
            {"id": 77, "run_number": 9, "conclusion": None, "head_sha": sha(1)},
            {"id": 6, "run_number": 6, "conclusion": "success", "head_sha": PREV},
            {"id": 4, "run_number": 4, "conclusion": "success", "head_sha": OLD},
        ]
        api = FakeApi([x, *commits], {**pulls, sha(20): []}, runs=runs)
        code, _, _ = run_check(api, event(before=sha(20)))
        self.assertEqual(code, 1)
        self.assertIn(f"/compare/{PREV}...{sha(1)}", api.paths("/compare/"))
        body = api.created[0]["body"]
        self.assertIn("korjaus [skip ci]", body)
        self.assertIn("Vertailupohja", body)

    def test_previous_run_not_ancestor_falls_back_to_before(self):
        commits, pulls = merge_push()
        runs = [{"id": 6, "run_number": 6, "conclusion": "failure", "head_sha": PREV}]
        api = FakeApi(commits, pulls, runs=runs, prev_status="diverged")
        code, _, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertIn(f"/compare/{BEFORE}...{sha(1)}", api.paths("/compare/"))
        self.assertIn("ei ole before-arvon esivanhempi", out)

    def test_runs_api_failure_falls_back_to_before(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, pulls, fail={"/actions/": 403})
        code, _, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertIn("Edellisten vahtiajojen luku epäonnistui", out)

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
        decoy = {"number": 3, "title": "Suora push mainiin", "user": {"login": "joku"}}
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
        api = FakeApi([commit(1, [BEFORE], github=True)], {sha(1): [pr(3, merged=False)]})
        self.assertEqual(run_check(api)[0], 1)

    def test_pr_merged_into_other_branch_is_not_enough(self):
        api = FakeApi([commit(1, [BEFORE], github=True)], {sha(1): [pr(3, base="develop")]})
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
        self.assertEqual(api.paths("/actions/"), [], "pakotetussa pushissa pohja on before")

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
        self.assertIn("yli tarkistusrajan", api.created[0]["body"])

    def test_large_merge_with_delayed_association_is_retried_up_to_limit(self):
        commits, pulls = merge_push(pr_commits=vahti.RETRY_LIMIT + 1)
        for c in commits:
            pulls[c["sha"]] = []
        api = FakeApi(commits, pulls)
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(sleep.delays, [], "yli rajan ei uusita")

    def test_issue_failure_still_fails_the_run(self):
        api = FakeApi([commit(1, [BEFORE])], {sha(1): []}, fail={"/issues": 410})
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("Issuen avaus tai päivitys epäonnistui", out)

    def test_untrusted_text_cannot_mention_or_inject(self):
        title = "::error::huijaus @juha `x` " + "a" * 200
        api = FakeApi([commit(1, [BEFORE], title=title, author="@toinen")], {sha(1): []})
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        outside_code = re.sub(r"`[^`]*`", "", body)
        self.assertNotIn("@", outside_code)
        self.assertNotIn("::error::", outside_code)
        self.assertTrue(all(line.startswith("main-push-vahti: ") for line in out.splitlines()))


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
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)

    def test_association_failure_is_error_and_reported(self):
        commits, pulls = merge_push()
        api = FakeApi(commits, fail={"/commits/": 500})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2)
        self.assertEqual(len(api.created), 1)

    def test_association_failure_after_found_reason_alarms(self):
        api = FakeApi([commit(1, [BEFORE])], fail={"/commits/": 500})
        self.assertEqual(run_check(api)[0], 1)

    def test_invalid_sha_is_error(self):
        api = FakeApi()
        self.assertEqual(run_check(api, event(after="main"))[0], 2)
        self.assertEqual(run_check(api, event(after=sha(1) + "\n"))[0], 2)
        self.assertEqual(run_check(api, event(before="x" * 40))[0], 2)
        self.assertEqual(api.paths("/compare/"), [])

    def test_main_requires_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(vahti.main(), 2)

    def test_unexpected_exception_is_not_an_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(event()), encoding="utf-8")
            env = {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "x"}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(vahti, "run", side_effect=KeyError("number")):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                        self.assertEqual(vahti.main(), 2)
        self.assertIn("odottamaton virhe KeyError", out.getvalue())

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

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = self.client(Recorder()).request(
                "GET", f"/commits/{sha(1)}/pulls", {"per_page": 100}
            )
        self.assertEqual(data, [{"number": 1}])
        req, timeout = seen[0]
        self.assertEqual(
            req.full_url, f"https://api.github.com/repos/{REPO}/commits/{sha(1)}/pulls?per_page=100"
        )
        self.assertEqual(req.get_header("X-github-api-version"), vahti.API_VERSION)
        self.assertEqual(req.get_header("Authorization"), "Bearer kokeilu")
        self.assertEqual(timeout, 30)

    def test_client_error_is_not_retried(self):
        sleep = Recorder()

        def fail(req, timeout):
            raise http_error(req, 404)

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(vahti.ApiError) as ctx:
                self.client(sleep).request("GET", "/compare/a...b")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(sleep.delays, [])

    def test_network_error_is_retried_then_raised(self):
        sleep = Recorder()

        def fail(req, timeout):
            raise urllib.error.URLError("katkos")

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(vahti.ApiError) as ctx:
                self.client(sleep).request("POST", "/issues", body={"title": "x"})
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(sleep.delays, [2, 5])

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

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(vahti.ApiError):
                self.client(sleep).request("GET", "/issues")
        self.assertEqual(sleep.delays, [])

    def test_non_json_body_is_api_error(self):
        with mock.patch("urllib.request.urlopen", lambda req, timeout: FakeResponse(b"<html>")):
            with self.assertRaises(vahti.ApiError) as ctx:
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
        self.assertEqual(re.findall(r"runs-on: (\S+)", self.text), ["ubuntu-latest"] * 2)

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
        self.assertIn("if: hashFiles('.github/scripts/main_push_vahti.py') == ''", self.text)

    def test_actions_are_pinned_to_sha(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertEqual(len(uses), 3)
        for ref in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        self.assertNotIn("secrets.", self.text)


if __name__ == "__main__":
    unittest.main()
