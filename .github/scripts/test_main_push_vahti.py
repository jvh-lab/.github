"""Main-push-vahdin fixture-testit, myös kielteiset tapaukset. Vain vakiokirjasto.

Ajo: python3 -m unittest discover -s .github/scripts -p test_main_push_vahti.py -v
"""

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
AFTER = "f" * 40
ZERO = "0" * 40


def sha(n: int) -> str:
    return (f"{n:x}" + "c" * 40)[:40]


def commit(n: int, title: str = "", author: str = "Testaaja") -> dict:
    return {
        "sha": sha(n),
        "commit": {"message": title or f"commit {n}\n\nrunko", "author": {"name": author}},
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
        "after": AFTER,
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": "https://github.com/jvh-lab/esimerkki/compare/aaaaaaa...fffffff",
        "pusher": {"name": "jvhprofileai-prog"},
    }
    data.update(overrides)
    return data


class FakeApi:
    """Rajapinnan korvike: vastaukset fixtureista, kutsut talteen."""

    def __init__(self, commits=None, pulls=None, status="ahead", issues=None, fail=None):
        self.commits = commits or []
        self.status = status
        # pulls: sha -> lista PR:iä tai lista vastauksia peräkkäisille kutsuille (callable-arvo).
        self.pulls = pulls or {}
        self.issues = issues or []
        self.fail = fail or {}
        self.calls = []
        self.created = []
        self.comments = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                raise vahti.ApiError(status, path)
        if path.startswith("/compare/"):
            page, size = params["page"], params["per_page"]
            chunk = self.commits[(page - 1) * size : page * size]
            return {"status": self.status, "total_commits": len(self.commits), "commits": chunk}
        if path.startswith("/commits/") and path.endswith("/pulls"):
            answer = self.pulls.get(path.split("/")[2], [])
            return answer() if callable(answer) else answer
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


def run_check(api, ev=None, delays=(1, 2)):
    sleep = Recorder()
    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        code = vahti.run(ev or event(), api, sleep, REPO, RUN_URL, None, delays)
    return code, sleep, out.getvalue()


class AcceptedPushes(unittest.TestCase):
    def test_merge_commit_and_pr_commits(self):
        commits = [commit(1), commit(2), commit(3, "Merge pull request #5 from jvh-lab/x")]
        api = FakeApi(commits, {sha(n): [pr(5)] for n in (1, 2, 3)})
        code, sleep, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.paths("/commits/")), 3)
        self.assertEqual(api.paths("/issues"), [])
        self.assertEqual(sleep.delays, [])
        self.assertIn("main-push-vahti: ok, tarkistettuja committeja 3", out)

    def test_squash_commit(self):
        api = FakeApi([commit(7, "Ominaisuus (#7)")], {sha(7): [pr(7)]})
        self.assertEqual(run_check(api)[0], 0)

    def test_rebase_merged_commits(self):
        commits = [commit(n) for n in (11, 12, 13)]
        api = FakeApi(commits, {sha(n): [pr(8)] for n in (11, 12, 13)})
        self.assertEqual(run_check(api)[0], 0)

    def test_commit_with_open_and_merged_pr(self):
        api = FakeApi([commit(1)], {sha(1): [pr(3, merged=False), pr(4)]})
        self.assertEqual(run_check(api)[0], 0)

    def test_delayed_association_is_retried(self):
        answers = iter([[], [pr(5)]])
        api = FakeApi([commit(1), commit(2)], {sha(1): [pr(5)], sha(2): lambda: next(answers)})
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(sleep.delays, [1])
        self.assertEqual(api.paths("/commits/").count(f"/commits/{sha(1)}/pulls"), 1)

    def test_compare_is_paginated(self):
        commits = [commit(n) for n in range(1, 151)]
        api = FakeApi(commits, {sha(n): [pr(9)] for n in range(1, 151)})
        code, _, out = run_check(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.paths("/compare/")), 2)
        self.assertIn("tarkistettuja committeja 150", out)


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
        api = FakeApi([commit(1, "korjaus suoraan mainiin")], {sha(1): []})
        code, sleep, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.created[0]["title"], "Suora push mainiin")
        body = api.created[0]["body"]
        self.assertIn(sha(1)[:12], body)
        self.assertIn("korjaus suoraan mainiin", body)
        self.assertIn(RUN_URL, body)
        self.assertEqual(sleep.delays, [1, 2])
        self.assertIn("Avattu issue #99", out)

    def test_open_issue_is_commented_not_duplicated(self):
        issues = [
            {"number": 4, "title": "Suora push mainiin", "pull_request": {}},
            {"number": 12, "title": "Suora push mainiin"},
            {"number": 6, "title": "Suora push mainiin"},
            {"number": 2, "title": "Muu asia"},
        ]
        api = FakeApi([commit(1)], {sha(1): []}, issues=issues)
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.created, [])
        self.assertEqual([c[0] for c in api.comments], ["/issues/6/comments"])
        self.assertIn("issueen #6", out)

    def test_issue_search_is_paginated(self):
        issues = [{"number": n, "title": f"asia {n}"} for n in range(1, 101)]
        issues.append({"number": 150, "title": "Suora push mainiin"})
        api = FakeApi([commit(1)], {sha(1): []}, issues=issues)
        self.assertEqual(run_check(api)[0], 1)
        self.assertEqual([c[0] for c in api.comments], ["/issues/150/comments"])

    def test_only_open_pr_is_not_enough(self):
        api = FakeApi([commit(1)], {sha(1): [pr(3, merged=False)]})
        self.assertEqual(run_check(api)[0], 1)

    def test_pr_merged_into_other_branch_is_not_enough(self):
        api = FakeApi([commit(1)], {sha(1): [pr(3, base="develop")]})
        self.assertEqual(run_check(api)[0], 1)

    def test_one_direct_commit_among_pr_commits(self):
        api = FakeApi([commit(1), commit(2, "suora")], {sha(1): [pr(5)], sha(2): []})
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        self.assertIn(sha(2)[:12], body)
        self.assertNotIn(sha(1)[:12], body)

    def test_forced_push_is_violation_even_with_pr_commits(self):
        api = FakeApi([commit(1)], {sha(1): [pr(5)]}, status="diverged")
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 1)
        self.assertIn("Pakotettu push", api.created[0]["body"])

    def test_forced_reset_to_older_commit(self):
        api = FakeApi([], status="behind")
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)

    def test_non_fast_forward_without_forced_flag(self):
        api = FakeApi([], status="behind")
        self.assertEqual(run_check(api)[0], 1)

    def test_too_many_commits_fails_closed(self):
        commits = [commit(n) for n in range(1, vahti.MAX_COMMITS + 2)]
        api = FakeApi(commits)
        code, _, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.paths("/commits/"), [])
        self.assertIn("yli tarkistusrajan", api.created[0]["body"])

    def test_large_direct_push_is_not_retried(self):
        commits = [commit(n) for n in range(1, vahti.RETRY_LIMIT + 2)]
        api = FakeApi(commits)
        code, sleep, _ = run_check(api)
        self.assertEqual(code, 1)
        self.assertEqual(sleep.delays, [])
        self.assertEqual(len(api.paths("/commits/")), vahti.RETRY_LIMIT + 1)

    def test_issue_failure_still_fails_the_run(self):
        api = FakeApi([commit(1)], {sha(1): []}, fail={"/issues": 410})
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        self.assertIn("Issuen avaus tai päivitys epäonnistui", out)

    def test_untrusted_text_cannot_mention_or_inject(self):
        title = "::error::huijaus @juha `x` " + "a" * 200
        api = FakeApi([commit(1, title, author="@toinen")], {sha(1): []})
        code, _, out = run_check(api)
        self.assertEqual(code, 1)
        body = api.created[0]["body"]
        outside_code = re.sub(r"`[^`]*`", "", body)
        self.assertNotIn("@", outside_code)
        self.assertNotIn("::error::", outside_code)
        self.assertTrue(all(line.startswith("main-push-vahti: ") for line in out.splitlines()))


class Errors(unittest.TestCase):
    def test_compare_failure_is_error_without_issue(self):
        api = FakeApi(fail={"/compare/": 404})
        code, _, _ = run_check(api)
        self.assertEqual(code, 2)
        self.assertEqual(api.created, [])

    def test_compare_failure_on_forced_push_still_alarms(self):
        api = FakeApi(fail={"/compare/": 404})
        code, _, _ = run_check(api, event(forced=True))
        self.assertEqual(code, 1)
        self.assertEqual(len(api.created), 1)

    def test_association_failure_is_error(self):
        api = FakeApi([commit(1)], fail={"/commits/": 500})
        self.assertEqual(run_check(api)[0], 2)

    def test_invalid_sha_is_error(self):
        api = FakeApi()
        self.assertEqual(run_check(api, event(after="main"))[0], 2)
        self.assertEqual(run_check(api, event(after=AFTER + "\n"))[0], 2)
        self.assertEqual(run_check(api, event(before="x" * 40))[0], 2)
        self.assertEqual(api.calls, [])

    def test_main_requires_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(vahti.main(), 2)

    def test_summary_is_written(self):
        api = FakeApi([commit(1)], {sha(1): []})
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


class RealClient(unittest.TestCase):
    def test_request_headers_and_url(self):
        seen = []

        def fake_urlopen(req, timeout):
            seen.append((req, timeout))
            return FakeResponse(json.dumps([{"number": 1}]).encode())

        client = vahti.GitHubApi("https://api.github.com/", REPO, "kokeilu", Recorder())
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            data = client.request("GET", f"/commits/{sha(1)}/pulls", {"per_page": 100})
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
        client = vahti.GitHubApi("https://api.github.com", REPO, "kokeilu", sleep)

        def fail(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(vahti.ApiError) as ctx:
                client.request("GET", "/compare/a...b")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(sleep.delays, [])

    def test_server_error_is_retried_then_raised(self):
        sleep = Recorder()
        client = vahti.GitHubApi("https://api.github.com", REPO, "kokeilu", sleep)

        def fail(req, timeout):
            raise urllib.error.URLError("katkos")

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(vahti.ApiError) as ctx:
                client.request("POST", "/issues", body={"title": "x"})
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(sleep.delays, [2, 5])


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
        self.assertEqual(
            grants,
            [
                ("contents", "read"),
                ("pull-requests", "read"),
                ("issues", "write"),
                ("contents", "read"),
            ],
        )

    def test_actions_are_pinned_to_sha(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        self.assertNotIn("secrets.", self.text)


if __name__ == "__main__":
    unittest.main()
