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

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "red_main_alert", ROOT / "scripts" / "red_main_alert.py")
rma = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rma)

ENV = {"GITHUB_REPOSITORY_OWNER": "owner", "ALERT_SMTP_USER": "alerts@example.test",
       "ALERT_SMTP_PASSWORD": "app-password", "ALERT_EMAIL_TO": "operator@example.test"}
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


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
    def __init__(self, runs, issue=None):
        self.runs, self.issue, self.log = runs, issue, []
    def workflow_runs(self, workflow, repo=None):
        self.read = (workflow, repo)
        return self.runs
    def open_issue(self, title):
        return self.issue if self.issue and self.issue["title"] == title else None
    def create_issue(self, title, body, owner):
        self.log.append(("create", title, owner))
        self.issue = {"number": 7, "title": title, "html_url": "https://example.test/issues/7"}
        return self.issue
    def comment(self, number, body):
        self.log.append(("comment", number, body))
    def close(self, number):
        self.log.append(("close", number))


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
    for runs in (GREEN, [], [_run("cancelled", "2026-11-10T11:00:00Z")]):
        gh = FakeGitHub(runs)
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
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None):
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
    text = (ROOT / ".github" / "workflows" / "red-main.yml").read_text()
    assert re.search(r"workflow_run:\s*\n\s*workflows: \[ci, origin-liveness\]", text), \
        "must run after every CI run and every origin-liveness run"
    assert "branches: [main]" in text
    assert re.search(r'- cron:\s*"\d+ \d+ \* \* \*"', text), "must repeat daily while red"
    assert "workflow_dispatch" in text and "--drill" in text, "must be drillable on demand"
    assert "scripts/red_main_alert.py" in text
    perms = text.split("permissions:", 1)[1].split("\n\n", 1)[0]
    assert "issues: write" in perms and "contents: read" in perms and "contents: write" not in text
    assert "git push" not in text and "git commit" not in text


def test_red_main_workflow_also_watches_the_public_verify_run():
    text = (ROOT / ".github" / "workflows" / "red-main.yml").read_text()
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
    def fake_run(gh, env, *, event, drill=False, target=None, watch_repo=None):
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
    wf = ROOT / ".github" / "workflows"
    name = re.search(r"^name:\s*(\S+)", (wf / "liveness.yml").read_text(), re.M).group(1)
    text = (wf / "red-main.yml").read_text()
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
        text = (ROOT / rel).read_text()
        assert not re.search(r"[\w.+-]+@[\w-]+\.(com|net|org|io)\b", text), rel
    wf = (ROOT / ".github" / "workflows" / "red-main.yml").read_text()
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in wf
