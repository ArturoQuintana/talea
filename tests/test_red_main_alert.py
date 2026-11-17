"""The red-main alert: a detector that must be able to fail, repeat, and heal.

Incident 2026-11-10: CI on main was red for 32 days with no alert. These tests
pin the behaviour that would have ended that on day one — the first red opens
an issue and emails; every day after, it says so again; the first green closes
it — and the failure modes of the alert itself (no email config, SMTP down)."""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import private_file

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "red_main_alert", ROOT / "scripts" / "red_main_alert.py")
rma = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rma)

ENV = {"GITHUB_REPOSITORY_OWNER": "owner", "ALERT_SMTP_USER": "alerts@example.test",
       "ALERT_SMTP_PASSWORD": "app-password", "ALERT_EMAIL_TO": "operator@example.test"}
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
NOW_Z = "2026-11-10T12:00:00Z"


def _run(conclusion: str, created: str, sha: str = "a" * 40) -> dict:
    return {"conclusion": conclusion, "created_at": created, "head_sha": sha,
            "html_url": f"https://example.test/runs/{sha[:4]}-{created}"}


RED_32_DAYS = [_run("failure", "2026-11-10T11:00:00Z", "c" * 40),
               _run("cancelled", "2026-11-09T11:00:00Z"),
               _run("failure", "2026-10-20T11:00:00Z"),
               _run("failure", "2026-10-09T15:01:13Z", "b" * 40),
               _run("success", "2026-10-09T10:30:31Z")]
GREEN = [_run("success", "2026-11-11T00:20:00Z", "d" * 40)] + RED_32_DAYS


class FakeGitHub:
    """`issue` is the open issue, if any; `filed` the issues already filed
    (open or closed) that the late check reads. Issues it creates are filed
    too, numbered 7, 8, …"""
    def __init__(self, runs, issue=None, filed=()):
        self.runs, self.issue, self.log = runs, issue, []
        if issue:                                 # open since long before any run here
            issue.setdefault("created_at", "2026-01-01T00:00:00Z")
            issue.setdefault("closed_at", None)
        for i in filed:                           # filed and closed at NOW unless said
            i.setdefault("created_at", NOW_Z)
            i.setdefault("closed_at", NOW_Z)
        self.filed = list(filed) + ([issue] if issue else [])
    def workflow_runs(self, workflow, repo=None, enough=None):
        self.read = (workflow, repo)
        return self.runs
    def issues_since(self, title, since):
        self.since = since
        return [i for i in self.filed if i["title"] == title     # as GitHub: updated since
                and rma._at(i.get("updated_at") or i["closed_at"] or NOW_Z) >= since]
    def open_issue(self, title):
        return next((i for i in self.filed if i["title"] == title and i.get("closed_at") is None), None)
    def fault_issues(self):                                # the label listing, not the cuttable one
        if getattr(self, "faults_fail", None):
            raise self.faults_fail
        return [i for i in self.filed if i.get("closed_at") is None
                and rma.FAULT_LABEL in (i.get("labels") or [])]
    def ensure_fault_label(self):
        self.log.append(("label", rma.FAULT_LABEL))
        if getattr(self, "label_fails", None):
            raise self.label_fails
    def add_labels(self, number, labels):
        self.log.append(("add-label", number))
        if getattr(self, "add_fails", None):
            raise self.add_fails
        issue = next(i for i in self.filed if i.get("number") == number)
        if not getattr(self, "add_ignored", False):
            issue["labels"] = list(issue.get("labels") or []) + list(labels)
        return [{"name": l} for l in issue["labels"]]
    def create_issue(self, title, body, owner, labels=None):
        self.log.append(("create", title, owner))
        n = 6 + sum(1 for e in self.log if e[0] == "create")
        self.issue = {"number": n, "title": title, "body": body, "created_at": NOW_Z,
                      "closed_at": None, "html_url": f"https://example.test/issues/{n}",
                      "labels": [] if getattr(self, "label_dropped", False) else list(labels or [])}
        self.filed.append(self.issue)
        return self.issue
    def comment(self, number, body):
        self.log.append(("comment", number, body))
    def close(self, number):
        self.log.append(("close", number))
        for i in self.filed:
            if i.get("number") == number:
                i["closed_at"] = NOW_Z
        if self.issue and self.issue["number"] == number:
            self.issue = None


class FakeSMTP:
    sent: list = []
    fail = False
    def __init__(self, host, port, timeout=None):
        assert (host, port) == ("smtp.gmail.com", 587)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def starttls(self):
        pass
    def login(self, user, password):
        if FakeSMTP.fail:
            raise OSError("smtp down")
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail = [], False


def _go(gh, event, env=ENV, **kw):
    return rma.run(gh, env, event=event, now=NOW, _smtp=FakeSMTP, **kw)


# --- the state of main ---

def test_main_state_finds_the_start_of_the_red_streak():
    s = rma.main_state(RED_32_DAYS)
    assert s["red"] and s["failed_runs"] == 3          # the cancelled run says nothing
    assert s["first_red"]["created_at"] == "2026-10-09T15:01:13Z"
    assert s["latest"]["head_sha"] == "c" * 40
    assert rma.days_red(s["first_red"]["created_at"], NOW) == 31


def test_main_state_green_and_undecided():
    assert rma.main_state(GREEN) == {"red": False, "latest": GREEN[0]}
    assert rma.main_state([_run("cancelled", "2026-11-10T11:00:00Z")]) is None
    assert rma.main_state([]) is None


# --- what it does, case by case ---

def test_first_red_opens_an_issue_and_emails_at_once():
    """Day one of the 32: the push that turned main red must produce both."""
    gh = FakeGitHub(RED_32_DAYS[3:])
    assert _go(gh, "workflow_run") == 0
    assert gh.log == [("create", rma.TITLE, "owner")]    # assigned -> GitHub notifies
    msg, = FakeSMTP.sent
    assert msg["Subject"].startswith("talea CI RED on main")
    assert msg["To"] == "operator@example.test"
    assert msg["Cc"] == msg["From"] == "alerts@example.test"   # the agents' mailbox gets a copy
    assert msg.get_content().startswith("CI on main is failing. Open the failing run")
    assert "https://example.test/issues/7" in msg.get_content()


def test_still_red_is_quiet_on_every_push_but_repeats_every_day():
    """main takes several pushes a day: one reminder a day, not one per push —
    but it must not go silent after the first notice, which is how an alert
    gets ignored for weeks."""
    issue = {"number": 7, "title": rma.TITLE, "html_url": "https://example.test/issues/7"}
    gh = FakeGitHub(RED_32_DAYS, issue)
    assert _go(gh, "workflow_run") == 0
    assert gh.log == [] and FakeSMTP.sent == []
    assert _go(gh, "schedule") == 0
    (kind, number, body), = gh.log
    assert (kind, number) == ("comment", 7) and "day 31" in body and "3 failed runs" in body
    msg, = FakeSMTP.sent
    assert msg["Subject"] == "talea CI still RED on main — day 31"
    assert "STILL failing" in msg.get_content()


def test_first_green_closes_the_issue_and_says_so():
    issue = {"number": 7, "title": rma.TITLE, "html_url": "https://example.test/issues/7"}
    gh = FakeGitHub(GREEN, issue)
    assert _go(gh, "workflow_run") == 0
    assert [e[0] for e in gh.log] == ["comment", "close"]
    msg, = FakeSMTP.sent
    assert msg["Subject"].startswith("talea CI green again on main")


def test_green_with_no_issue_and_undecided_do_nothing():
    """Green with no issue open, and the red streak before it already reported
    (an issue, closed, names its run): nothing to do. (A streak no issue names
    is reported late: the T-38 tests below.)"""
    reported = {"title": rma.TITLE, "body": f"- Latest failing run: {GREEN[1]['html_url']}"}
    for runs in (GREEN, [], [_run("cancelled", "2026-11-10T11:00:00Z")]):
        gh = FakeGitHub(runs, filed=[reported])
        assert _go(gh, "schedule") == 0
        assert gh.log == [] and FakeSMTP.sent == []


# --- the alert's own failure modes ---

def test_without_email_secrets_the_issue_still_files(capsys):
    """Before the secrets are set (or if one is removed) the GitHub channel
    must keep working, and the run must say the email was skipped."""
    gh = FakeGitHub(RED_32_DAYS[3:])
    assert _go(gh, "workflow_run", env={"GITHUB_REPOSITORY_OWNER": "owner"}) == 0
    assert gh.log == [("create", rma.TITLE, "owner")] and FakeSMTP.sent == []
    assert "email not configured" in capsys.readouterr().out


def test_smtp_failure_fails_the_run_after_the_issue_is_filed(capsys):
    """A swallowed email failure is how an alert dies silently: the issue is
    filed first, then the run exits non-zero so the failure is visible."""
    FakeSMTP.fail = True
    gh = FakeGitHub(RED_32_DAYS[3:])
    assert _go(gh, "workflow_run") == 1
    assert gh.log == [("create", rma.TITLE, "owner")]
    assert "EMAIL-FAILED" in capsys.readouterr().out


def test_drill_proves_both_channels_and_cleans_up():
    gh = FakeGitHub(GREEN)
    assert _go(gh, "workflow_dispatch", drill=True) == 0
    assert [e[0] for e in gh.log] == ["create", "comment", "close"]
    assert gh.log[0][1] == rma.DRILL_TITLE
    msg, = FakeSMTP.sent
    assert msg["Subject"].startswith("[DRILL]") and "Nothing is wrong" in msg.get_content()
    assert gh.log[1][2] == "Drill email sent."
    FakeSMTP.fail = True
    assert _go(FakeGitHub(GREEN), "workflow_dispatch", drill=True) == 1


def test_drill_fails_when_email_is_not_configured(capsys):
    """Found in the first real drill (2026-11-10): the drill treated missing
    email settings as a pass and commented "sent (or email not configured)". A
    drill exists to prove delivery — if a secret is removed or mistyped it must
    fail, visibly, and still clean up its issue. (A REAL red is different: there
    the issue must file even when email is not set up.)"""
    gh = FakeGitHub(GREEN)
    assert _go(gh, "workflow_dispatch", env={"GITHUB_REPOSITORY_OWNER": "owner"},
               drill=True) == 1
    assert [e[0] for e in gh.log] == ["create", "comment", "close"]
    assert "NOT sent" in gh.log[1][2] and FakeSMTP.sent == []
    assert "EMAIL-NOT-CONFIGURED" in capsys.readouterr().out


# --- the REST client and the entry point ---

def test_github_client_speaks_the_expected_rest_calls():
    calls = []
    class Resp:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.payload
    def fake_open(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req.data, req.headers["Authorization"]))
        if "/actions/workflows/ci.yml/runs" in req.full_url:
            return Resp(b'{"workflow_runs": [{"conclusion": "success"}]}')
        if req.get_method() == "GET":
            return Resp(b'[{"title": "x", "pull_request": {}}, {"title": "CI is red on main", "number": 3}]')
        if req.get_method() == "POST" and req.full_url.endswith("/issues"):
            return Resp(b'{"number": 9, "html_url": "u"}')
        return Resp(b"")
    gh = rma.GitHub("o/r", "tok", api="https://api.example.test", _open=fake_open)
    assert gh.ci_runs() == [{"conclusion": "success"}]
    assert gh.workflow_runs("ci.yml") == [{"conclusion": "success"}]
    assert gh.open_issue(rma.TITLE)["number"] == 3 and gh.open_issue("nope") is None
    assert gh.create_issue("t", "b", "owner")["number"] == 9
    gh.comment(9, "c"); gh.close(9)
    methods = [(m, u.replace("https://api.example.test/repos/o/r", "")) for m, u, *_ in calls]
    assert ("POST", "/issues/9/comments") in methods and ("PATCH", "/issues/9") in methods
    assert "branch=main" in calls[0][1] and all(c[3] == "Bearer tok" for c in calls)
    assert b'"assignees": ["owner"]' in next(c[2] for c in calls if c[1].endswith("/issues") and c[0] == "POST")


def test_main_reads_the_workflow_environment(monkeypatch):
    seen = {}
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None, trigger=None):
        seen.update(repo=gh.repo, event=event, drill=drill)
        seen["target"], seen["watch_repo"] = target, watch_repo
        return 0
    monkeypatch.setattr(rma, "run", fake_run)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert rma.main([]) == 0 and seen == {"repo": "o/r", "event": "schedule", "drill": False,
                                          "target": rma.CI, "watch_repo": None}
    assert rma.main(["--drill"]) == 0 and seen["drill"] is True
    assert rma.main(["--target", "mirror-verify", "--repo", "o/mirror"]) == 0
    assert seen["target"] is rma.MIRROR_VERIFY and seen["watch_repo"] == "o/mirror"
    assert seen["repo"] == "o/r", "the issue is still filed in the workflow's own repo"
    with pytest.raises(SystemExit):
        rma.main(["--target", "mirror-verify"])      # no mirror named: refuse, do not guess


# --- the second target: the public mirror's verify run (D-10, 2026-11-11) ---

def _go_mirror(gh, event, **kw):
    return _go(gh, event, target=rma.MIRROR_VERIFY, watch_repo="o/mirror", **kw)


def test_a_red_public_verify_run_opens_its_own_issue_and_emails():
    gh = FakeGitHub(RED_32_DAYS)
    assert _go_mirror(gh, "workflow_run") == 0
    assert gh.read == ("verify.yml", "o/mirror"), "must read the MIRROR's verify runs"
    assert gh.log == [("create", rma.MIRROR_VERIFY.title, "owner")]
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == "talea public verify RED on the mirror — ccccccc"
    assert "public mirror's verify run is failing" in msg.get_content()
    assert "CI on main" not in msg.get_content()


def test_the_two_targets_do_not_share_an_issue():
    """A red CI issue being open must not make a red verify run look already
    reported (and the reverse): each target finds only its own title."""
    ci_issue = {"number": 3, "title": rma.CI.title, "html_url": "https://example.test/issues/3"}
    gh = FakeGitHub(RED_32_DAYS, issue=ci_issue)
    assert _go_mirror(gh, "workflow_run") == 0
    assert gh.log[0] == ("create", rma.MIRROR_VERIFY.title, "owner")
    assert rma.MIRROR_VERIFY.title != rma.CI.title


def test_public_verify_repeats_daily_and_heals_like_ci():
    issue = {"number": 7, "title": rma.MIRROR_VERIFY.title, "html_url": "https://example.test/issues/7"}
    gh = FakeGitHub(RED_32_DAYS, issue=dict(issue))
    assert _go_mirror(gh, "workflow_run") == 0 and gh.log == [] and FakeSMTP.sent == []
    assert _go_mirror(gh, "schedule") == 0
    assert gh.log[0][0] == "comment"
    body = FakeSMTP.sent[0].get_content()
    assert "STILL failing — day 31" in body and "README badge shows failing" in body
    assert "resolver PR" not in body
    gh2 = FakeGitHub(GREEN, issue=dict(issue))
    assert _go_mirror(gh2, "workflow_run") == 0
    assert [a[0] for a in gh2.log] == ["comment", "close"]
    assert FakeSMTP.sent[-1]["Subject"] == "talea public verify green again on the mirror — ddddddd"


def test_ci_wording_is_unchanged_by_the_second_target():
    gh = FakeGitHub(RED_32_DAYS)
    assert _go(gh, "workflow_run") == 0
    assert gh.read == ("ci.yml", None)
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == "talea CI RED on main — ccccccc"
    assert msg.get_content().startswith("CI on main is failing.")


def test_mirror_runs_are_read_without_the_token_when_github_refuses_it():
    """The workflow's token belongs to the private repo. If GitHub refuses it on
    the mirror, the public run list is read with no token; a refusal on the
    workflow's OWN repo is a real error and must not be swallowed."""
    import urllib.error
    calls = []
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"workflow_runs": [{"conclusion": "failure"}]}'
    def fake_open(req, timeout=None):
        authed = "Authorization" in req.headers
        calls.append((req.full_url, authed))
        if authed:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return Resp()
    gh = rma.GitHub("o/r", "tok", api="https://api.example.test", _open=fake_open)
    assert gh.workflow_runs("verify.yml", "o/mirror") == [{"conclusion": "failure"}]
    assert [a for _, a in calls] == [True, False]
    assert all("/repos/o/mirror/actions/workflows/verify.yml/runs" in u for u, _ in calls)
    with pytest.raises(urllib.error.HTTPError):
        gh.workflow_runs("ci.yml")

    def server_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
    gh500 = rma.GitHub("o/r", "tok", api="https://api.example.test", _open=server_error)
    with pytest.raises(urllib.error.HTTPError):
        gh500.workflow_runs("verify.yml", "o/mirror")     # not an auth refusal: do not mask it


# --- the workflow that runs it ---

def test_red_main_workflow_is_wired_and_read_only():
    text = private_file(".github/workflows/red-main.yml").read_text()
    assert re.search(r"workflow_run:\s*\n\s*workflows: \[ci, origin-liveness, mirror-freshness, receipt-push-times\]",
                     text), "must run after every CI, origin-liveness, mirror-freshness and receipt-push-times run"
    assert "branches: [main]" in text
    assert re.search(r'- cron:\s*"\d+ \d+ \* \* \*"', text), "must repeat daily while red"
    assert "workflow_dispatch" in text and "--drill" in text, "must be drillable on demand"
    assert "scripts/red_main_alert.py" in text
    perms = text.split("permissions:", 1)[1].split("\n\n", 1)[0]
    assert "issues: write" in perms and "contents: read" in perms and "contents: write" not in text
    assert "git push" not in text and "git commit" not in text


def test_red_main_workflow_also_watches_the_public_verify_run():
    text = private_file(".github/workflows/red-main.yml").read_text()
    step = text.split("Report the public mirror's verify state", 1)[1]
    assert "--target mirror-verify --repo ArturoQuintana/talea" in step
    assert "always()" in step, "a failed CI-alert step must not silence the mirror check"
    assert "!inputs.drill" in step, "a drill is one test email, not two reports"
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in step


# --- the third target: the origin liveness run (incident 2026-11-03..09) ---

def test_a_red_liveness_run_opens_its_own_issue_and_emails_what_to_look_for():
    """The detector is a scheduled run: red, it tells nobody unless this alert
    does. Its issue must not be confused with the CI or mirror ones, it reads
    THIS repo's runs (no watch_repo), and the reminder says what red means."""
    gh = FakeGitHub([_run("failure", "2026-11-13T17:41:00Z")])
    assert _go(gh, "workflow_run", target=rma.LIVENESS) == 0
    assert gh.log == [("create", rma.LIVENESS.title, "owner")]
    assert gh.read == ("liveness.yml", None)
    assert len({rma.CI.title, rma.MIRROR_VERIFY.title, rma.LIVENESS.title}) == 3
    assert "origin liveness RED on origin" in FakeSMTP.sent[-1]["Subject"]
    still = rma.build_email("email_still_red", {"days": 2, "failed_runs": 2},
                            sender="a@example.test", to="b@example.test",
                            target=rma.LIVENESS)
    assert "the run log names it" in still.get_content()
    assert "diverged" in still.get_content()     # the cause the server cannot see


def test_liveness_target_is_selectable_and_reads_this_repo(monkeypatch):
    seen = {}
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None, trigger=None):
        seen.update(target=target, watch_repo=watch_repo)
        return 0
    monkeypatch.setattr(rma, "run", fake_run)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert rma.main(["--target", "liveness"]) == 0
    assert seen == {"target": rma.LIVENESS, "watch_repo": None}
    assert rma.LIVENESS.workflow == "liveness.yml"


def test_red_main_workflow_also_reports_the_liveness_run():
    """Both halves of the wiring: red-main must be TRIGGERED by the detector's
    runs (its `name:` must match the workflow_run list) and must have a step
    that reads them. Either half missing and a stale origin alerts nobody."""
    # by file, not by folder: the mirror has a .github/workflows of its own (verify.yml)
    name = re.search(r"^name:\s*(\S+)",
                     private_file(".github/workflows/liveness.yml").read_text(), re.M).group(1)
    text = private_file(".github/workflows/red-main.yml").read_text()
    listed = re.search(r"workflow_run:\s*\n\s*workflows: \[([^\]]*)\]", text).group(1)
    assert name in [w.strip() for w in listed.split(",")]
    step = text.split("Report the origin liveness state", 1)[1]
    assert "--target liveness" in step
    assert "always()" in step and "!inputs.drill" in step
    for secret in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in step


def test_no_address_or_credential_is_written_into_the_alert_code():
    """scripts/ is published to the public mirror: the operator's address, the
    alert account and its password come from secrets only."""
    for rel in ("scripts/red_main_alert.py", ".github/workflows/red-main.yml"):
        text = (private_file(rel) if rel.startswith(".github/") else ROOT / rel).read_text()
        assert not re.search(r"[\w.+-]+@[\w-]+\.(com|net|org|io)\b", text), rel
    wf = private_file(".github/workflows/red-main.yml").read_text()
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in wf


# --- the fourth target: the public mirror's freshness (register T-28) ---

def test_a_red_freshness_run_opens_its_own_issue_and_says_what_red_means():
    gh = FakeGitHub([_run("failure", "2026-11-16T12:45:00Z")])
    assert _go(gh, "workflow_run", target=rma.FRESHNESS) == 0
    assert gh.log == [("create", rma.FRESHNESS.title, "owner")]
    assert gh.read == ("mirror-freshness.yml", None)
    assert len({t.title for t in rma.TARGETS.values()}) == len(rma.TARGETS) == 5
    assert "mirror freshness RED on the mirror" in FakeSMTP.sent[-1]["Subject"]
    still = rma.build_email("email_still_red", {"days": 1, "failed_runs": 3},
                            sender="a@example.test", to="b@example.test", target=rma.FRESHNESS)
    body = still.get_content()
    # red means one of four things; the title asserts none of them, the reminder names
    # each, and says where to tell them apart
    assert rma.FRESHNESS.title == "The public mirror's freshness check is red"
    assert "Red can mean any of these; the run log is the authority on which." in body
    assert "STALE: the public mirror has missed two publish windows in a row" in body
    assert "server's journal" in body
    assert "CANNOT JUDGE: the check could not determine freshness" in body
    assert "the time-zone data was missing" in body
    assert "A failed step before the check" in body and "the check did not run" in body
    assert "The next successful run of the check closes this issue." in body


def _freshness_reminder() -> str:
    return rma.build_email("email_still_red", {"days": 1, "failed_runs": 2}, sender="a@example.test",
                           to="b@example.test", target=rma.FRESHNESS).get_content()


def test_a_freshness_drill_is_not_reported_as_a_stale_mirror():
    """mirror-freshness.yml's own drill fails on purpose, and red-main cannot see
    that input (its `!inputs.drill` guard is its own). So nothing red-main sends
    may say the mirror is stale; and since a drill can run while the mirror IS
    stale, the DRILL sentence may not say the mirror is fine either."""
    gh = FakeGitHub([_run("failure", "2026-11-16T12:50:00Z")])
    assert _go(gh, "workflow_run", target=rma.FRESHNESS) == 0
    first = FakeSMTP.sent[-1]
    assert "stale" not in first["Subject"].lower() and "stale" not in rma.FRESHNESS.title.lower()
    assert "is failing" in first.get_content() and "stale" not in first.get_content().lower()
    body = _freshness_reminder()
    assert "DRILL: a run started with drill=true fails on purpose; it says nothing about the mirror." in body
    assert "nothing is wrong" not in body.lower()


def test_a_stale_run_after_a_drill_keeps_the_issue_open():
    """Only a successful run closes the issue: a drill followed by a real
    failure (and a drill run while the mirror is stale) leaves it open and
    reminds, as the reminder's last sentence promises."""
    issue = {"number": 7, "title": rma.FRESHNESS.title, "html_url": "https://example.test/issues/7"}
    for runs in ([_run("failure", "2026-11-16T13:15:00Z"), _run("failure", "2026-11-16T12:50:00Z")],
                 [_run("failure", "2026-11-16T12:50:00Z"), _run("failure", "2026-11-16T12:45:00Z")]):
        gh = FakeGitHub(runs, dict(issue))
        assert _go(gh, "schedule", target=rma.FRESHNESS) == 0
        assert [e[0] for e in gh.log] == ["comment"]
    assert "The next successful run of the check closes this issue." in _freshness_reminder()


def test_a_green_freshness_run_after_a_drill_closes_the_issue():
    """A drill run fails on purpose; the next successful run closes the issue,
    with its email."""
    issue = {"number": 7, "title": rma.FRESHNESS.title, "html_url": "https://example.test/issues/7"}
    gh = FakeGitHub([_run("success", "2026-11-16T13:15:00Z"), _run("failure", "2026-11-16T12:50:00Z")],
                    issue)
    assert _go(gh, "workflow_run", target=rma.FRESHNESS) == 0
    assert [e[0] for e in gh.log] == ["comment", "close"]
    assert FakeSMTP.sent[-1]["Subject"].startswith("talea mirror freshness green again on the mirror")


def test_freshness_target_is_selectable_and_reads_this_repo(monkeypatch):
    seen = {}

    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None, trigger=None):
        seen.update(target=target, watch_repo=watch_repo)
        return 0
    monkeypatch.setattr(rma, "run", fake_run)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert rma.main(["--target", "freshness"]) == 0
    assert seen == {"target": rma.FRESHNESS, "watch_repo": None}


def test_red_main_workflow_also_reports_the_freshness_run():
    """Both halves of the wiring, as for liveness: triggered by the detector's
    runs (by its `name:`), and a step that reads them."""
    name = re.search(r"^name:\s*(\S+)",
                     private_file(".github/workflows/mirror-freshness.yml").read_text(), re.M).group(1)
    text = private_file(".github/workflows/red-main.yml").read_text()
    listed = re.search(r"workflow_run:\s*\n\s*workflows: \[([^\]]*)\]", text).group(1)
    assert name in [w.strip() for w in listed.split(",")]
    step = text.split("Report the public mirror's freshness", 1)[1]
    assert "--target freshness" in step
    assert "always()" in step and "!inputs.drill" in step
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in step



# --- a failure a newer green run hid (register T-38, 2026-11-16) ---
# red-main read the NEWEST decisive run, so a failed run F followed by a green
# run G before F's red-main job read GitHub opened no issue and emailed nobody.
# Worse, red-main's concurrency group keeps one pending job: F's job can be
# cancelled and never run. Every red-main run now reports, late and in order,
# each red streak of the last week that a success ended and no issue names.

S = _run("success", "2026-11-09T10:00:00Z", "5" * 40)        # green before
F = _run("failure", "2026-11-10T11:00:00Z", "f" * 40)        # the failure
G = _run("success", "2026-11-10T11:30:00Z", "e" * 40)        # green again, before F's job read


def _kinds(gh):
    return [e[0] for e in gh.log]


def _late_report_of_f(gh, first_email=0):
    """F was reported late and in order: the issue opened naming F, the red
    email, then the issue closed at G and the green email."""
    assert gh.log[0] == ("create", rma.TITLE, "owner")
    assert gh.log[1][0] == "comment" and G["html_url"] in gh.log[1][2]
    assert gh.log[2] == ("close", 7)
    late = next(i for i in gh.filed if i.get("number") == 7)
    assert F["html_url"] in late["body"] and "reported late" in late["body"]
    red, green = FakeSMTP.sent[first_email:first_email + 2]
    assert red["Subject"] == "talea CI RED on main — fffffff"
    assert "First red run: 2026-11-10T11:00:00Z (commit fffffff)." in red.get_content()
    assert "Found late" in red.get_content()
    assert green["Subject"] == "talea CI green again on main — eeeeeee"


def test_fs_notification_after_g_has_completed_reports_f_late_and_in_order():
    """The regression the register asks for: F's own red-main job runs after G
    has completed, so the newest run it reads is green."""
    gh = FakeGitHub([G, F, S])
    assert _go(gh, "workflow_run", trigger=F) == 0
    assert _kinds(gh) == ["create", "comment", "close"] and len(FakeSMTP.sent) == 2
    _late_report_of_f(gh)
    assert _go(gh, "workflow_run", trigger=G) == 0          # G's own job: told once, not twice
    assert _kinds(gh) == ["create", "comment", "close"] and len(FakeSMTP.sent) == 2


@pytest.mark.parametrize("target", [rma.CI, rma.LIVENESS, rma.FRESHNESS], ids=["ci", "liveness", "freshness"])
def test_fs_job_cancelled_while_pending_loses_nothing(target):
    """Only G's job runs (F's pending job was cancelled by the concurrency
    group): F is still reported. For the freshness check this includes a drill
    run, which fails on purpose: a drill whose alert was lost is reported late,
    which is what a drill is for."""
    gh = FakeGitHub([G, F, S])
    assert _go(gh, "workflow_run", trigger=G, target=target) == 0
    assert _kinds(gh) == ["create", "comment", "close"]
    assert gh.log[0][1] == target.title and F["html_url"] in gh.filed[-1]["body"]
    red, green = FakeSMTP.sent
    assert red["Subject"].startswith(f"talea {target.name} RED") and "green again" in green["Subject"]


def test_the_polled_mirror_verify_is_caught_up_by_the_daily_schedule():
    """mirror-verify has no triggering run here (it is read on the public
    repository): the daily schedule finds the hidden failure."""
    gh = FakeGitHub([G, F, S])
    assert _go(gh, "schedule", target=rma.MIRROR_VERIFY, watch_repo="o/mirror") == 0
    assert _kinds(gh) == ["create", "comment", "close"] and len(FakeSMTP.sent) == 2
    assert gh.read == ("verify.yml", "o/mirror") and gh.log[0][1] == rma.MIRROR_VERIFY.title


def test_a_triggering_run_the_listing_does_not_show_yet_counts():
    gh = FakeGitHub([S])
    assert _go(gh, "workflow_run", trigger=F) == 0
    assert _kinds(gh) == ["create"]                         # red now: the ordinary first-red path
    msg, = FakeSMTP.sent
    assert "Found late" not in msg.get_content() and "A reminder follows once a day" in msg.get_content()
    assert rma.with_trigger([S], dict(F, conclusion="cancelled")) == [S]
    assert rma.with_trigger([G, F, S], F) == [G, F, S]


def test_an_older_hidden_streak_is_told_before_the_current_red():
    F2 = _run("failure", "2026-11-10T11:50:00Z", "2" * 40)
    gh = FakeGitHub([F2, G, F, S])
    assert _go(gh, "workflow_run", trigger=F2) == 0
    assert _kinds(gh) == ["create", "comment", "close", "create"]
    _late_report_of_f(gh)
    third = FakeSMTP.sent[2]
    assert third["Subject"] == "talea CI RED on main — 2222222" and len(FakeSMTP.sent) == 3
    assert gh.open_issue(rma.TITLE)["number"] == 8


def test_a_streak_any_issue_names_or_older_than_the_lookback_is_not_told_again():
    named = {"title": rma.TITLE, "body": f"- Latest failing run: {F['html_url']}"}
    gh = FakeGitHub([G, F, S], filed=[named])
    assert _go(gh, "schedule") == 0 and gh.log == [] and FakeSMTP.sent == []
    old = _run("failure", "2026-11-02T11:00:00Z", "0" * 40)     # 8 days before NOW
    gh = FakeGitHub([G, old, S])
    assert _go(gh, "schedule") == 0 and gh.log == [] and FakeSMTP.sent == []
    assert gh.since == NOW - rma.timedelta(days=rma.LOOKBACK_DAYS)
    other = {"title": rma.LIVENESS.title, "body": f"{F['html_url']}"}   # another target's issue
    gh = FakeGitHub([G, F, S], filed=[other])
    assert _go(gh, "schedule") == 0 and _kinds(gh) == ["create", "comment", "close"]


def test_a_flapping_workflow_sends_at_most_three_late_reports_and_one_summary():
    """Five hidden streaks: the three oldest are told one by one, the other
    two in ONE closed issue and ONE email; the next run tells nothing."""
    runs = []
    for i in range(5):                                       # oldest first, then reversed
        runs += [_run("failure", f"2026-11-0{5 + i}T10:00:00Z", str(i) * 40),
                 _run("success", f"2026-11-0{5 + i}T11:00:00Z", "9" * 40)]
    runs = list(reversed(runs)) + [S]
    gh = FakeGitHub(runs)
    assert _go(gh, "schedule") == 0
    assert _kinds(gh) == ["create", "comment", "close"] * 3 + ["create", "close"]
    assert len(FakeSMTP.sent) == 7
    firsts = [m.get_content() for m in FakeSMTP.sent[0:6:2]]
    assert [f.split("(commit ")[1][:7] for f in firsts] == ["0000000", "1111111", "2222222"]
    summary = FakeSMTP.sent[-1]
    assert summary["Subject"] == "talea CI: 2 more red streaks on main were never reported"
    assert "The oldest began at 2026-11-08T10:00:00Z" in summary.get_content()
    assert all(r["html_url"] in gh.filed[-1]["body"] for r in runs if r["head_sha"][0] in "34")
    assert _go(gh, "schedule") == 0 and len(FakeSMTP.sent) == 7 and len(gh.log) == 11


def test_the_first_red_line_names_the_first_red_runs_commit():
    """Codex's note on the mirror-freshness pull request: "First red run: <time> (commit …)" printed the
    NEWEST run's commit. It is the first red run's own."""
    gh = FakeGitHub(RED_32_DAYS[:4] + RED_32_DAYS[4:])
    assert _go(gh, "workflow_run") == 0
    msg, = FakeSMTP.sent
    assert msg["Subject"] == "talea CI RED on main — ccccccc"                # the newest
    assert "First red run: 2026-10-09T15:01:13Z (commit bbbbbbb)." in msg.get_content()


def test_the_triggering_run_is_read_from_the_event_payload(tmp_path):
    event = tmp_path / "event.json"
    wr = {"id": 42, "conclusion": "failure", "created_at": F["created_at"], "head_sha": F["head_sha"],
          "html_url": F["html_url"], "path": ".github/workflows/ci.yml", "repository": {"full_name": "o/r"}}
    event.write_text(rma.json.dumps({"workflow_run": wr}))
    env = {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r"}
    assert rma.triggering_run(env, rma.CI, None) == {k: wr[k] for k in ("id", "conclusion", "created_at",
                                                                         "head_sha", "html_url")}
    assert rma.triggering_run(env, rma.LIVENESS, None) is None             # another target's run
    assert rma.triggering_run(env, rma.MIRROR_VERIFY, "o/mirror") is None  # the mirror is polled
    assert rma.triggering_run(dict(env, GITHUB_EVENT_NAME="schedule"), rma.CI, None) is None
    assert rma.triggering_run(dict(env, GITHUB_EVENT_PATH=str(tmp_path / "missing")), rma.CI, None) is None
    event.write_text("not json")
    assert rma.triggering_run(env, rma.CI, None) is None


def test_main_hands_the_triggering_run_to_run(monkeypatch, tmp_path):
    seen = {}
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None, trigger=None):
        seen["trigger"] = trigger
        return 0
    event = tmp_path / "event.json"
    event.write_text(rma.json.dumps({"workflow_run": {
        "conclusion": "failure", "html_url": "u", "created_at": "t", "head_sha": "s", "id": 1,
        "path": ".github/workflows/ci.yml", "repository": {"full_name": "o/r"}}}))
    monkeypatch.setattr(rma, "run", fake_run)
    for k, v in (("GITHUB_REPOSITORY", "o/r"), ("GITHUB_TOKEN", "tok"),
                 ("GITHUB_EVENT_NAME", "workflow_run"), ("GITHUB_EVENT_PATH", str(event))):
        monkeypatch.setenv(k, v)
    assert rma.main([]) == 0 and seen["trigger"]["html_url"] == "u"


def test_the_github_client_reads_issues_of_one_title_since_a_time():
    calls = []
    gh = rma.GitHub("o/r", "tok")
    gh.call = lambda method, path, body=None, **kw: calls.append((method, path)) or [
        {"title": rma.TITLE, "body": "x"}, {"title": rma.TITLE, "pull_request": {}}, {"title": "other"}]
    assert gh.issues_since(rma.TITLE, NOW) == [{"title": rma.TITLE, "body": "x"}]
    assert calls == [("GET", "/issues?state=all&since=2026-11-10T12:00:00Z&per_page=100&page=1")]


def test_a_streak_that_begins_before_the_listing_says_its_start_may_be_earlier():
    """GitHub lists the newest 100 runs: a streak with no success before it in
    the listing may have begun earlier than its oldest listed run."""
    gh = FakeGitHub([G, F])
    assert _go(gh, "schedule") == 0 and _kinds(gh) == ["create", "comment", "close"]
    assert "First red run: 2026-11-10T11:00:00Z or earlier (commit fffffff)." in FakeSMTP.sent[0].get_content()
    assert "First red run: 2026-11-10T11:00:00Z or earlier" in gh.filed[-1]["body"]


# --- round 2: exact URLs, open-interval coverage, pagination, failed deliveries ---

def _r(conclusion, created, run_id, sha="a" * 40):
    return {"conclusion": conclusion, "created_at": created, "head_sha": sha,
            "html_url": f"https://example.test/runs/{run_id}"}


def test_a_url_that_only_starts_like_a_reported_runs_does_not_count():
    """Run 123 is not run 1234: an issue naming /runs/1234 does not cover a
    failure at /runs/123 (Codex, round 1)."""
    f, g, s = (_r("failure", "2026-11-10T11:00:00Z", 123, "f" * 40),
               _r("success", "2026-11-10T11:30:00Z", 125), _r("success", "2026-11-09T10:00:00Z", 100))
    other = {"title": rma.TITLE, "body": "- Latest failing run: https://example.test/runs/1234\n"}
    gh = FakeGitHub([g, f, s], filed=[other])
    assert _go(gh, "schedule") == 0 and _kinds(gh) == ["create", "comment", "close"]
    assert rma.named_urls("see https://example.test/runs/1234. and (https://example.test/runs/9)") == {
        "https://example.test/runs/1234", "https://example.test/runs/9"}


def test_an_issue_open_at_any_instant_of_the_streak_covers_it():
    streak, ended = [F], G
    def issue(created, closed):
        return {"title": rma.TITLE, "body": "", "created_at": created, "closed_at": closed}
    assert rma.covered(streak, ended, [issue("2026-11-10T11:10:00Z", "2026-11-10T11:40:00Z")])
    assert rma.covered(streak, ended, [issue("2026-11-01T00:00:00Z", None)])           # still open
    assert rma.covered(streak, ended, [issue("2026-11-10T11:30:00Z", "2026-11-10T11:30:00Z")])  # edges
    assert not rma.covered(streak, ended, [issue("2026-11-01T00:00:00Z", "2026-11-10T10:59:59Z")])
    assert not rma.covered(streak, ended, [issue("2026-11-10T11:30:01Z", None)])         # after it ended


def test_a_red_while_an_incident_is_open_is_covered_and_an_earlier_one_is_still_told():
    """Codex, round 1, point 4: with an issue open, the backfill still runs. A streak
    during the open incident is covered by it; a streak that ended before the
    issue was opened is told late, while the issue stays open."""
    F0 = _r("failure", "2026-11-08T10:00:00Z", 10, "0" * 40)
    G0 = _r("success", "2026-11-08T11:00:00Z", 11)
    F1 = _r("failure", "2026-11-09T10:00:00Z", 12, "1" * 40)
    G1 = _r("success", "2026-11-09T11:00:00Z", 13)
    F2 = _r("failure", "2026-11-10T10:00:00Z", 14, "2" * 40)
    open_since_f1 = {"number": 3, "title": rma.TITLE, "html_url": "https://example.test/issues/3",
                     "created_at": "2026-11-09T10:05:00Z", "closed_at": None}
    gh = FakeGitHub([F2, G1, F1, G0, F0, S], dict(open_since_f1))
    assert _go(gh, "workflow_run") == 0
    assert _kinds(gh) == ["create", "comment", "close"]              # F0 only, told late
    assert "https://example.test/runs/10" in gh.filed[-1]["body"]
    assert "https://example.test/runs/12" not in gh.filed[-1]["body"]


def test_a_long_streak_whose_runs_fell_off_the_listing_is_not_told_again():
    """Codex, round 1, point 2: a reported 32-day streak's first URLs are no longer
    listed; the issue that was open through it covers what is left."""
    tail = [_r("failure", f"2026-11-{d:02d}T10:00:00Z", 200 + d) for d in range(4, 11)]
    green = _r("success", "2026-11-10T23:00:00Z", 300)
    was_open = {"title": rma.TITLE, "body": "- Latest failing run: https://example.test/runs/1",
                "created_at": "2026-10-09T15:05:00Z", "closed_at": "2026-11-10T23:05:00Z"}
    gh = FakeGitHub([green] + list(reversed(tail)), filed=[was_open])
    assert _go(gh, "schedule") == 0 and gh.log == [] and FakeSMTP.sent == []


def test_a_failed_late_red_email_stops_there_and_the_next_run_finishes():
    """Codex, round 1, point 6: the red email fails, so there is no comment, no close
    and no green email; the run fails; nothing more is told late. The issue stays
    open; the next run closes it on the ordinary path and tells the next streak."""
    F2 = _r("failure", "2026-11-10T11:40:00Z", 21, "2" * 40)
    G2 = _r("success", "2026-11-10T11:50:00Z", 22, "3" * 40)
    gh = FakeGitHub([G2, F2, G, F, S])
    FakeSMTP.fail = True
    assert _go(gh, "schedule") == 1
    assert _kinds(gh) == ["create"] and FakeSMTP.sent == []
    FakeSMTP.fail = False
    assert _go(gh, "schedule") == 0
    assert _kinds(gh) == ["create", "create", "comment", "close", "comment", "close"]
    assert "https://example.test/runs/21" in gh.filed[1]["body"]       # F2, told late
    assert gh.log[-2][1] == 7 and gh.log[-1] == ("close", 7)          # the first one, closed
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert subjects == ["talea CI RED on main — 2222222", "talea CI green again on main — 3333333",
                        "talea CI green again on main — 3333333"]


def test_a_failed_summary_email_leaves_its_issue_open():
    runs = []
    for i in range(4):
        runs += [_r("failure", f"2026-11-0{5 + i}T10:00:00Z", 40 + 2 * i, str(i) * 40),
                 _r("success", f"2026-11-0{5 + i}T11:00:00Z", 41 + 2 * i)]
    gh = FakeGitHub(list(reversed(runs)) + [S])
    real = FakeSMTP.send_message
    def fail_the_summary(self, msg):
        if "more red streaks" in msg["Subject"]:
            raise OSError("smtp down")
        real(self, msg)
    FakeSMTP.send_message = fail_the_summary
    try:
        assert _go(gh, "schedule") == 1
    finally:
        FakeSMTP.send_message = real
    assert _kinds(gh) == ["create", "comment", "close"] * 3 + ["create"]     # never closed
    assert gh.issue["number"] == 10 and "https://example.test/runs/46" in gh.issue["body"]


def test_the_current_red_says_or_earlier_when_no_success_precedes_it():
    gh = FakeGitHub([_r("failure", "2026-11-10T11:00:00Z", 51, "f" * 40),
                     _r("failure", "2026-11-10T10:00:00Z", 50, "e" * 40)])
    assert _go(gh, "workflow_run") == 0
    msg, = FakeSMTP.sent
    assert "First red run: 2026-11-10T10:00:00Z or earlier (commit eeeeeee)." in msg.get_content()
    assert "First red run: 2026-11-10T10:00:00Z or earlier" in gh.filed[-1]["body"]


def _paged(pages, calls):
    def call(method, path, body=None, repo=None, auth=True):
        calls.append((path, auth))
        n = int(path.rsplit("page=", 1)[1])
        if isinstance(pages[n - 1], Exception):
            raise pages[n - 1]
        return pages[n - 1]
    return call


def test_the_run_history_is_read_page_by_page_until_it_is_enough():
    def page(day, conclusion="failure", n=100):
        return {"workflow_runs": [_r(conclusion, f"2026-11-{day:02d}T10:00:00Z", day * 1000 + k)
                                  for k in range(n)]}
    since = datetime(2026, 11, 3, tzinfo=timezone.utc)
    gh, calls = rma.GitHub("o/r", "tok"), []
    gh.call = _paged([page(9), page(5), page(2, "success"), page(1)], calls)
    runs = gh.workflow_runs("ci.yml", enough=lambda rs: rma.enough_history(rs, since))
    assert len(runs) == 300 and [p.rsplit("page=", 1)[1] for p, _ in calls] == ["1", "2", "3"]
    calls.clear()
    gh.call = _paged([page(9), page(8, n=40)], calls)                # a short page ends it
    assert len(gh.workflow_runs("ci.yml", enough=lambda rs: False)) == 140 and len(calls) == 2
    calls.clear()
    gh.call = _paged([page(9)] * 20, calls)
    with pytest.raises(rma.HistoryCut) as cut:              # the cap: never taken as complete
        gh.workflow_runs("ci.yml", enough=lambda rs: False)
    assert len(cut.value.runs) == 100 * rma.MAX_PAGES and len(calls) == rma.MAX_PAGES
    calls.clear()
    gh.call = _paged([page(9)], calls)
    assert len(gh.workflow_runs("ci.yml")) == 100 and len(calls) == 1      # one page by default


def test_the_mirrors_pages_are_read_without_the_token_once_github_refuses_it():
    refused = rma.urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    pages = {1: [refused, {"workflow_runs": [_r("failure", "2026-11-09T10:00:00Z", k) for k in range(100)]}],
             2: [{"workflow_runs": [_r("success", "2026-11-01T10:00:00Z", 999)]}]}
    calls = []
    def call(method, path, body=None, repo=None, auth=True):
        calls.append((int(path.rsplit("page=", 1)[1]), auth))
        got = pages[calls[-1][0]].pop(0)
        if isinstance(got, Exception):
            raise got
        return got
    gh = rma.GitHub("o/r", "tok")
    gh.call = call
    runs = gh.workflow_runs("verify.yml", "o/mirror", enough=lambda rs: False)    # page 2 is short: the end
    assert len(runs) == 101 and calls == [(1, True), (1, False), (2, False)]


def test_the_issues_are_read_page_by_page():
    mine = {"title": rma.TITLE, "number": 150, "body": ""}
    pages = [[{"title": "other", "number": k} for k in range(100)],
             [{"title": "other", "number": 100, "pull_request": {}}, mine]]
    calls = []
    gh = rma.GitHub("o/r", "tok")
    gh.call = lambda method, path, body=None, **kw: calls.append(path) or pages[int(path.rsplit("page=", 1)[1]) - 1]
    assert gh.open_issue(rma.TITLE) == mine
    assert calls == ["/issues?state=open&per_page=100&page=1", "/issues?state=open&per_page=100&page=2"]
    assert gh.issues_since(rma.TITLE, NOW) == [mine]


def test_enough_history_needs_the_lookback_and_a_success_at_its_start():
    since = datetime(2026, 11, 3, tzinfo=timezone.utc)
    new_red, old_red = _r("failure", "2026-11-09T10:00:00Z", 1), _r("failure", "2026-11-01T10:00:00Z", 2)
    old_green = _r("success", "2026-11-01T09:00:00Z", 3)
    assert not rma.enough_history([], since)
    assert not rma.enough_history([new_red], since)                       # not back to the lookback
    assert not rma.enough_history([new_red, old_red], since)              # the oldest streak is open
    assert rma.enough_history([new_red, old_red, old_green], since)
    assert not rma.enough_history([new_red, _r("cancelled", "2026-11-01T10:00:00Z", 4)], since)


def test_an_open_issue_not_updated_lately_still_covers_its_incident():
    """GitHub's `since` is the last update: an issue opened long ago and not
    touched since is not in that listing. It is open, so it is read as the
    open issue, and it covers a streak that ended during it."""
    quiet = {"number": 4, "title": rma.TITLE, "html_url": "https://example.test/issues/4",
             "created_at": "2026-10-01T00:00:00Z", "closed_at": None, "updated_at": "2026-10-01T00:00:00Z"}
    gh = FakeGitHub([G, F, S], dict(quiet))
    assert gh.issues_since(rma.TITLE, NOW - rma.timedelta(days=7)) == []
    assert _go(gh, "schedule") == 0
    assert _kinds(gh) == ["comment", "close"]               # the ordinary close, nothing told late


def test_the_history_is_read_past_a_success_inside_the_lookback():
    """A success inside the lookback does not end the reading: older failures in
    the lookback lie behind it."""
    def page(day, conclusion, n=100):
        return {"workflow_runs": [_r(conclusion, f"2026-11-{day:02d}T10:00:00Z", day * 1000 + k)
                                  for k in range(n)]}
    since = datetime(2026, 11, 3, tzinfo=timezone.utc)
    gh, calls = rma.GitHub("o/r", "tok"), []
    gh.call = _paged([page(9, "failure"), page(8, "success"), page(5, "failure"),
                      page(2, "success"), page(1, "failure")], calls)
    runs = gh.workflow_runs("ci.yml", enough=lambda rs: rma.enough_history(rs, since))
    assert len(calls) == 4 and len(runs) == 400



# --- round 3 (Codex, round 2) ---

def test_a_two_failure_streak_is_covered_by_an_issue_open_only_near_its_first_failure():
    """The streak's first failure is its EARLIEST, whatever the order it is
    held in: an issue open from just after the first failure to before the
    second covers the streak."""
    first = _r("failure", "2026-11-10T10:00:00Z", 60, "1" * 40)
    second = _r("failure", "2026-11-10T11:00:00Z", 61, "2" * 40)
    green = _r("success", "2026-11-10T11:30:00Z", 62)
    near_first = {"title": rma.TITLE, "body": "", "created_at": "2026-11-10T10:05:00Z",
                  "closed_at": "2026-11-10T10:30:00Z"}
    assert rma.covered([first, second], green, [near_first])
    assert rma.covered([second, first], green, [near_first])
    gh = FakeGitHub([green, second, first, S], filed=[dict(near_first)])
    assert _go(gh, "schedule") == 0 and gh.log == [] and FakeSMTP.sent == []


class CutGitHub(FakeGitHub):
    """A GitHub whose reads hit their page cap: `where` is "runs", "issues" or "open"."""
    def __init__(self, runs, where, **kw):
        super().__init__(runs, **kw)
        self.where = where
    def workflow_runs(self, workflow, repo=None, enough=None):
        if self.where == "runs":
            raise rma.HistoryCut(f"the run history of {workflow}", self.runs)
        return super().workflow_runs(workflow, repo, enough)
    def issues_since(self, title, since):
        if self.where == "issues":
            raise rma.HistoryCut("the issues")
        return super().issues_since(title, since)
    def open_issue(self, title):
        if self.where == "open":
            raise rma.HistoryCut("the issues")
        return super().open_issue(title)


HISTORY = "red-main could not read enough history for CI"


def _cut_email():
    msg = FakeSMTP.sent[-1]
    assert msg["Subject"] == "talea red-main could not read enough history for CI"
    assert msg.get_content().startswith(
        "red-main could not read enough history for CI; alerts may be missing.")
    return msg.get_content()


def _fault_log(gh):
    return [e for e in gh.log if e[0] != "label"]


def test_a_run_history_cut_by_the_page_cap_is_a_fault_with_its_own_issue():
    """Codex, round 2: reaching the cap is never "complete". No late report is
    made from a cut history; the current state is still judged FIRST; the
    fault is told through its own labelled issue (one email); the run fails."""
    gh = CutGitHub([G, F, S], "runs")
    assert _go(gh, "schedule") == 1
    assert _fault_log(gh) == [("create", HISTORY, "owner")]     # F is NOT told late from a cut history
    assert gh.issue["labels"] == [rma.FAULT_LABEL] and ("label", rma.FAULT_LABEL) in gh.log
    assert "What could not be read in full: the run history of ci.yml." in _cut_email()
    assert len(FakeSMTP.sent) == 1
    red_now = CutGitHub([F, S], "runs")
    FakeSMTP.sent = []
    assert _go(red_now, "workflow_run") == 1
    assert [e[1] for e in _fault_log(red_now)] == [rma.TITLE, HISTORY]   # the red FIRST, then the fault
    assert len(FakeSMTP.sent) == 2 and FakeSMTP.sent[0]["Subject"].startswith("talea CI RED")


def test_issues_cut_by_the_page_cap_skip_the_late_reports_and_fail():
    gh = CutGitHub([G, F, S], "issues")
    assert _go(gh, "schedule") == 1 and _fault_log(gh) == [("create", HISTORY, "owner")]
    assert "What could not be read in full: the issues." in _cut_email()


def test_an_open_issue_that_cannot_be_known_stops_everything_but_the_fault():
    gh = CutGitHub([F, S], "open")
    assert _go(gh, "workflow_run") == 1 and _fault_log(gh) == [("create", HISTORY, "owner")]
    assert len(FakeSMTP.sent) == 1 and _cut_email()


def test_a_cut_with_no_decisive_run_is_still_told():
    """Codex, round 3: ten full pages of cancelled runs read as "no decisive
    run" returned 0 before the cut was told."""
    cancelled = [_r("cancelled", f"2026-11-10T10:{m:02d}:00Z", 900 + m) for m in range(5)]
    gh = CutGitHub(cancelled, "runs")
    assert _go(gh, "schedule") == 1
    assert _fault_log(gh) == [("create", HISTORY, "owner")] and len(FakeSMTP.sent) == 1


def test_the_cut_fault_is_told_once_reminded_daily_and_closed_once():
    """Codex, round 3: no email on every invocation. First run: the issue and
    one email. Same day again: nothing. The daily schedule: one comment and
    one reminder. A run that reads enough: the issue closed, one email."""
    gh = CutGitHub([G, F, S], "runs")
    assert _go(gh, "workflow_run") == 1 and len(FakeSMTP.sent) == 1
    assert _go(gh, "workflow_run") == 1 and len(FakeSMTP.sent) == 1 and len(_fault_log(gh)) == 1
    assert _go(gh, "schedule") == 1 and len(FakeSMTP.sent) == 2
    assert gh.log[-1][0] == "comment" and "Still cannot read enough history" in gh.log[-1][2]
    assert FakeSMTP.sent[-1]["Subject"] == "talea red-main still cannot read enough history for CI"
    gh.where = None                                        # history readable again
    gh.filed.append({"title": rma.TITLE, "body": F["html_url"]})   # F was told meanwhile
    gh.filed[-1].setdefault("created_at", NOW_Z); gh.filed[-1].setdefault("closed_at", NOW_Z)
    assert _go(gh, "workflow_run") == 0
    assert gh.log[-2][0] == "comment" and gh.log[-1] == ("close", 7)
    ok = FakeSMTP.sent[-1]
    assert ok["Subject"] == "talea red-main reads enough history again for CI"
    assert "history readable again" in ok.get_content() and len(FakeSMTP.sent) == 3
    assert _go(gh, "workflow_run") == 0 and len(FakeSMTP.sent) == 3        # closed: nothing more


def test_a_fault_listing_that_fails_never_stops_the_ordinary_red():
    """Codex, round 4: the fault path cannot suppress the target's own alert.
    The label listing fails: the red still opens its issue and emails; about
    the fault nothing is done (no issue, no email); the run fails."""
    gh = FakeGitHub([F, S])
    gh.faults_fail = rma.urllib.error.URLError("down")
    assert _go(gh, "workflow_run") == 1
    assert gh.log == [("create", rma.TITLE, "owner")] and len(FakeSMTP.sent) == 1
    cut = CutGitHub([F, S], "runs")
    cut.faults_fail = OSError("down")
    FakeSMTP.sent = []
    assert _go(cut, "workflow_run") == 1
    assert _fault_log(cut) == [("create", rma.TITLE, "owner")] and len(FakeSMTP.sent) == 1


def test_an_ordinary_alert_that_fails_never_stops_the_fault_path():
    class Broken(CutGitHub):
        def create_issue(self, title, body, owner, labels=None):
            if title == rma.TITLE:
                raise OSError("issue API down")
            return super().create_issue(title, body, owner, labels)
    gh = Broken([F, S], "runs")
    assert _go(gh, "workflow_run") == 1
    assert _fault_log(gh) == [("create", HISTORY, "owner")] and len(FakeSMTP.sent) == 1


def test_the_recovery_email_never_says_nothing_to_do():
    """Codex, round 4: history can become readable while the target is red."""
    body = rma.build_email("email_history_ok", {"run_url": "u", "issue_url": "i"},
                           sender="a@example.test", to="b@example.test").get_content()
    assert "nothing to do" not in body.lower() and "history readable again" in body
    gh = FakeGitHub([F, S], filed=[{"number": 3, "title": HISTORY, "labels": [rma.FAULT_LABEL],
                                    "html_url": "https://example.test/issues/3", "closed_at": None}])
    assert _go(gh, "workflow_run") == 0
    red, readable = FakeSMTP.sent                         # the target is red, history readable again
    assert red["Subject"].startswith("talea CI RED")
    assert readable["Subject"] == "talea red-main reads enough history again for CI"
    assert all("nothing to do" not in m.get_content().lower() for m in FakeSMTP.sent)


def test_a_label_that_cannot_be_confirmed_opens_nothing_and_emails_nothing_every_run(capsys):
    """Codex, round 5: fail closed. Without the label, a fault issue could not
    be found by a later run and would be opened and emailed again each time.
    So: no issue, no email, exit 1, run after run; and the target's own alert
    is untouched. When history is readable again: nothing to close, exit 0."""
    gh = CutGitHub([G, F, S], "runs")
    gh.label_fails = OSError("labels API down")
    for _ in range(3):
        assert _go(gh, "workflow_run") == 1
    assert [e for e in gh.log if e[0] == "create"] == [] and FakeSMTP.sent == []
    assert f"the label {rma.FAULT_LABEL} cannot be confirmed (OSError)" in capsys.readouterr().out
    gh.where = None
    gh.filed.append({"title": rma.TITLE, "body": F["html_url"], "created_at": NOW_Z, "closed_at": NOW_Z})
    assert _go(gh, "workflow_run") == 0 and FakeSMTP.sent == []


def test_an_issue_returned_without_the_label_gets_it_added():
    gh = CutGitHub([G, F, S], "runs")
    gh.label_dropped = True
    assert _go(gh, "workflow_run") == 1
    assert ("add-label", 7) in gh.log and gh.issue["labels"] == [rma.FAULT_LABEL]
    assert len(FakeSMTP.sent) == 1                               # the one fault email
    assert _go(gh, "workflow_run") == 1                         # found by label: no second issue
    assert [e for e in gh.log if e[0] == "create"] == [("create", HISTORY, "owner")]
    assert len(FakeSMTP.sent) == 1


def test_an_issue_that_cannot_carry_the_label_is_closed_at_once_without_email(capsys):
    for how in ("add_fails", "add_ignored"):
        FakeSMTP.sent = []
        gh = CutGitHub([G, F, S], "runs")
        gh.label_dropped = True
        setattr(gh, how, OSError("down") if how == "add_fails" else True)
        assert _go(gh, "workflow_run") == 1
        assert gh.log[-2] == ("close", 7) and gh.log[-1][0] == "comment"      # closed FIRST, then said
        assert gh.log[-1][2].startswith("Closed: the label") and FakeSMTP.sent == []
        out = capsys.readouterr().out
        assert out.index("closing it; no email is sent") < out.index("… it is closed")
        gh.where = None                                          # recovery: nothing open to close
        gh.filed.append({"title": rma.TITLE, "body": F["html_url"], "created_at": NOW_Z, "closed_at": NOW_Z})
        assert _go(gh, "workflow_run") == 0 and FakeSMTP.sent == []


def test_an_issue_that_cannot_be_labelled_nor_closed_sends_nothing_and_claims_no_closure(capsys):
    """Codex, round 6: if GitHub refuses the close, no comment claims it."""
    class Unclosable(CutGitHub):
        def close(self, number):
            raise OSError("close API down")
    gh = Unclosable([G, F, S], "runs")
    gh.label_dropped, gh.add_fails = True, OSError("down")
    assert _go(gh, "workflow_run") == 1 and FakeSMTP.sent == []
    out = capsys.readouterr().out
    assert "the close could not be confirmed (OSError); the issue may still be open" in out
    assert "closing it; no email is sent" in out
    assert "is closed" not in out and "closed and" not in out          # Codex, round 7: never claimed
    assert not [e for e in gh.log if e[0] == "comment"]


def test_a_failing_comment_never_stops_the_close_run_after_run(capsys):
    """Codex, round 6: the close is on its own; the comment that says why comes
    after it, and its failure changes nothing: every unlabelled issue is closed,
    no email, every run."""
    class Mute(CutGitHub):
        def comment(self, number, body):
            raise OSError("comment API down")
    gh = Mute([G, F, S], "runs")
    gh.label_dropped, gh.add_fails = True, OSError("down")
    for n in (7, 8, 9):
        assert _go(gh, "workflow_run") == 1
        assert gh.log[-1] == ("close", n)
    assert FakeSMTP.sent == []
    assert "closed, but the comment saying why failed (OSError)" in capsys.readouterr().out


def test_a_created_issue_without_a_number_sends_nothing(capsys):
    """Sonnet, round 6: it can be neither labelled nor closed nor found."""
    class Numberless(CutGitHub):
        def create_issue(self, title, body, owner, labels=None):
            issue = super().create_issue(title, body, owner, labels)
            return {k: v for k, v in issue.items() if k != "number"}
    gh = Numberless([G, F, S], "runs")
    assert _go(gh, "workflow_run") == 1 and FakeSMTP.sent == []
    assert "came back without a number" in capsys.readouterr().out
    assert not [e for e in gh.log if e[0] in ("add-label", "close", "comment")]


def test_the_fault_issues_are_listed_by_label_to_the_end():
    """No search: the open issues labelled red-main-fault, every page; a fault
    issue on page 2 is found, so no second one is opened."""
    mine = {"title": HISTORY, "number": 150, "labels": [{"name": rma.FAULT_LABEL}]}
    pages = [[{"title": "other fault", "number": k} for k in range(100)], [mine]]
    calls = []
    gh = rma.GitHub("o/r", "tok")
    gh.call = lambda method, path, body=None, **kw: calls.append((method, path, body)) or \
        pages[int(path.rsplit("page=", 1)[1]) - 1]
    assert rma._open_fault(gh, HISTORY) == mine
    assert [c[1] for c in calls] == [f"/issues?state=open&labels={rma.FAULT_LABEL}&per_page=100&page=1",
                                     f"/issues?state=open&labels={rma.FAULT_LABEL}&per_page=100&page=2"]
    assert rma._labelled(mine) and not rma._labelled({"labels": ["x"]}) and rma._labelled({"labels": [rma.FAULT_LABEL]})
    calls.clear()
    pages = [[{"title": "other", "number": 1}] * 100] * 30
    with pytest.raises(rma.HistoryCut):
        gh.fault_issues()                                  # never read to its end: never taken as complete
    calls.clear()
    gh.call = lambda method, path, body=None, **kw: calls.append((method, path, body)) or {}
    gh.create_issue("t", "b", "owner", labels=[rma.FAULT_LABEL])
    assert calls[-1][2]["labels"] == [rma.FAULT_LABEL]
    gh.create_issue("t", "b", "owner")
    assert "labels" not in calls[-1][2]


def test_the_label_is_read_then_made_and_anything_else_is_not_confirmed():
    def http(code):
        return rma.urllib.error.HTTPError("u", code, "x", {}, None)
    gh, calls = rma.GitHub("o/r", "tok"), []
    gh.call = lambda method, path, body=None, **kw: calls.append((method, path)) or {"name": rma.FAULT_LABEL}
    gh.ensure_fault_label()
    assert calls == [("GET", f"/labels/{rma.FAULT_LABEL}")]                  # exists: nothing made
    for post, ok in ((None, True), (http(422), True), (http(403), False)):
        calls = []
        def call(method, path, body=None, **kw):
            calls.append((method, path))
            if method == "GET":
                raise http(404)
            if post:
                raise post
            return {}
        gh.call = call
        if ok:
            gh.ensure_fault_label()
        else:
            with pytest.raises(rma.urllib.error.HTTPError):
                gh.ensure_fault_label()
        assert calls == [("GET", f"/labels/{rma.FAULT_LABEL}"), ("POST", "/labels")]
    gh.call = lambda method, path, body=None, **kw: (_ for _ in ()).throw(http(500))
    with pytest.raises(rma.urllib.error.HTTPError):
        gh.ensure_fault_label()                                            # unreadable: not confirmed
    seen = []
    gh.call = lambda method, path, body=None, **kw: seen.append((method, path, body)) or [{"name": "x"}]
    assert gh.add_labels(7, [rma.FAULT_LABEL]) == [{"name": "x"}]
    assert seen == [("POST", "/issues/7/labels", {"labels": [rma.FAULT_LABEL]})]
    gh.call = lambda method, path, body=None, **kw: None
    assert gh.add_labels(7, [rma.FAULT_LABEL]) == []


def test_search_is_gone():
    code = (ROOT / "scripts" / "red_main_alert.py").read_text()
    assert "/search/" not in code and "find_open_issue" not in code


def test_the_issues_reader_never_returns_a_cut_listing():
    gh = rma.GitHub("o/r", "tok")
    gh.call = lambda method, path, body=None, **kw: [{"title": "other", "number": 1}] * 100
    with pytest.raises(rma.HistoryCut):
        gh.open_issue(rma.TITLE)


def test_the_summary_email_does_not_say_closed_before_the_close():
    body = rma.build_email("email_late_summary", {"streaks": 2, "issue_url": "u"},
                           sender="a@example.test", to="b@example.test").get_content()
    assert "(closed)" not in body and "(being closed once this email is sent)" in body


def test_the_summary_says_or_earlier_when_its_oldest_run_is_the_oldest_read():
    """Sonnet, round 2. Through run() the oldest streaks are told one by one
    first, so the summary's oldest is only the oldest read if that changes;
    the wording is pinned on the function itself."""
    streak = ([_r("failure", "2026-11-05T10:00:00Z", 70, "7" * 40)], _r("success", "2026-11-05T11:00:00Z", 71))
    for known, says in ((True, "The oldest began at 2026-11-05T10:00:00Z:"),
                        (False, "The oldest began at 2026-11-05T10:00:00Z or earlier:")):
        FakeSMTP.sent = []
        gh = FakeGitHub([])
        assert rma.report_late_summary(gh, ENV, [streak], owner="owner", target=rma.CI,
                                       start_known=known, _smtp=FakeSMTP)
        assert says in FakeSMTP.sent[-1].get_content()


def test_a_fault_path_that_fails_after_the_red_is_reported_still_fails_the_run(capsys):
    class Broken(CutGitHub):
        def create_issue(self, title, body, owner, labels=None):
            if title == HISTORY:
                raise OSError("issue API down")
            return super().create_issue(title, body, owner, labels)
    gh = Broken([F, S], "runs")
    assert _go(gh, "workflow_run") == 1
    assert _fault_log(gh) == [("create", rma.TITLE, "owner")] and len(FakeSMTP.sent) == 1
    assert "the history fault could not be handled (OSError)" in capsys.readouterr().out


def test_a_fault_that_comes_back_after_its_issue_closed_opens_a_new_one():
    """Sonnet, round 4: cut, recovery (closed, one email), cut again: a NEW
    issue and one email, not silence."""
    gh = CutGitHub([G, F, S], "runs")
    assert _go(gh, "workflow_run") == 1 and len(FakeSMTP.sent) == 1
    gh.where = None
    gh.filed.append({"title": rma.TITLE, "body": F["html_url"], "created_at": NOW_Z, "closed_at": NOW_Z})
    assert _go(gh, "workflow_run") == 0 and len(FakeSMTP.sent) == 2 and gh.log[-1] == ("close", 7)
    gh.where = "runs"
    assert _go(gh, "workflow_run") == 1
    assert [e for e in _fault_log(gh) if e[0] == "create"] == [("create", HISTORY, "owner")] * 2
    assert gh.issue["number"] == 8 and len(FakeSMTP.sent) == 3
    assert FakeSMTP.sent[-1]["Subject"] == "talea red-main could not read enough history for CI"



# --- the fifth target: when each receipt reached GitHub (register T-35) ---

def test_a_red_push_times_run_opens_its_own_issue_and_says_what_red_means():
    gh = FakeGitHub([_run("failure", "2026-11-16T18:25:00Z")])
    assert _go(gh, "workflow_run", target=rma.PUSH_TIMES) == 0
    assert gh.log == [("create", "The receipt push-time check is red", "owner")]
    assert gh.read == ("receipt-push-times.yml", None)
    assert FakeSMTP.sent[-1]["Subject"].startswith("talea receipt push times RED on origin")
    body = rma.build_email("email_still_red", {"days": 1, "failed_runs": 2}, sender="a@example.test",
                           to="b@example.test", target=rma.PUSH_TIMES).get_content()
    assert "Red can mean either; the run log is the authority." in body
    assert "UNVERIFIABLE (exit 1, trigger T3)" in body and "CANNOT JUDGE (exit 2, trigger T4)" in body
    assert "this is not a leak finding" in body
    assert "A failed step before the check: the check did not run." in body
    assert "The next successful run closes this issue." in body
    assert "leak." not in rma.PUSH_TIMES.title.lower() and "leak" not in rma.PUSH_TIMES.title.lower()


def test_push_times_target_is_selectable_and_reads_this_repo(monkeypatch):
    seen = {}
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None, trigger=None):
        seen.update(target=target, watch_repo=watch_repo)
        return 0
    monkeypatch.setattr(rma, "run", fake_run)
    for k, v in (("GITHUB_REPOSITORY", "o/r"), ("GITHUB_TOKEN", "tok"), ("GITHUB_EVENT_NAME", "schedule")):
        monkeypatch.setenv(k, v)
    assert rma.main(["--target", "push-times"]) == 0
    assert seen == {"target": rma.PUSH_TIMES, "watch_repo": None}


def test_red_main_workflow_also_reports_the_push_times_run():
    text = private_file(".github/workflows/red-main.yml").read_text()
    step = text.split("- name: Report the receipt push times (issue + email)", 1)[1]
    assert "if: ${{ always() && !inputs.drill }}" in step.split("run:", 1)[0]
    assert "run: uv run --no-project python scripts/red_main_alert.py --target push-times" in step
