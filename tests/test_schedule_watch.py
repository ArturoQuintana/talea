"""The schedules target of the red-main alert: a scheduled workflow that stops running.

Every other red-main target judges runs that exist. A schedule that GitHub
disabled, dropped or never registered starts no run, so nothing turned red:
a detector could fall silent and nobody would know. These tests pin how a stop
is read (state, newest scheduled run, threshold from the workflow's own cron
lines), how it is told (one issue, a comment and email when the set of stopped
workflows changes, close when all run again), that an unreadable answer is a
fault and never "running", and the heartbeat step that watches red-main from
outside GitHub."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import private_file

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "red_main_alert", ROOT / "scripts" / "red_main_alert.py")
rma = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rma)

ENV = {"GITHUB_REPOSITORY_OWNER": "owner", "ALERT_SMTP_USER": "alerts@example.test",
       "ALERT_SMTP_PASSWORD": "app-password", "ALERT_EMAIL_TO": "operator@example.test",
       "GITHUB_SERVER_URL": "https://example.test", "GITHUB_REPOSITORY": "o/r",
       "GITHUB_RUN_ID": "99"}
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
MIRROR = "o/mirror"


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(hours: float) -> str:
    return _z(NOW - timedelta(hours=hours))


DAILY = 'on:\n  schedule:\n    - cron: "17 12 * * *"   # daily\n  workflow_dispatch:\n'
LADDER = ('on:\n  schedule:\n    - cron: "23 3 * * *"\n    - cron: \'23 7 * * *\'\n'
          '    - cron: "23 11 * * *"\n')


def _tree(tmp_path: Path, files: dict[str, str], verify: str | None = DAILY) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files.items():
        (wf / name).write_text("name: x\n" + text)
    if verify is not None:
        (tmp_path / "mirror").mkdir()
        (tmp_path / "mirror" / "verify.yml").write_text("name: verify\n" + verify)
    return tmp_path


class FakeSMTP:
    sent: list = []
    fail = False
    attempts = 0
    def __init__(self, host, port, timeout=None):
        assert (host, port) == ("smtp.gmail.com", 587)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def starttls(self):
        pass
    def login(self, user, password):
        FakeSMTP.attempts += 1
        if FakeSMTP.fail:
            raise OSError("smtp down")
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail, FakeSMTP.attempts = [], False, 0


class FakeGitHub:
    """`flows` maps (file, repo) to (GitHub's workflow record or None, the
    newest scheduled run or None); a value that is an Exception is raised."""
    def __init__(self, flows, issue=None, faults=(), comments=()):
        self.flows, self.issue, self.log = flows, issue, []
        self.faults, self.comments = list(faults), list(comments)
    def _get(self, file, repo, i):
        got = self.flows[(file, repo)]
        if isinstance(got, Exception):
            raise got
        return got[i]
    def workflow_info(self, file, repo=None):
        self.log.append(("info", file, repo))
        return self._get(file, repo, 0)
    def newest_scheduled_run(self, file, repo=None):
        return self._get(file, repo, 1)
    def open_issue(self, title):
        if getattr(self, "issues_fail", None):
            raise self.issues_fail
        assert title == rma.SCHEDULES.title
        return self.issue
    def create_issue(self, title, body, owner, labels=None):
        self.log.append(("create", title, owner))
        new = {"number": 7, "title": title, "body": body, "created_at": _z(NOW),
               "html_url": "https://example.test/issues/7", "labels": list(labels or [])}
        if labels:
            self.faults.append(new)
        else:
            self.issue = new
        return new
    def issue_comments(self, number):
        assert self.issue and number == self.issue["number"]
        return list(self.comments)
    def comment(self, number, body):
        if getattr(self, "comment_fails", None):
            raise self.comment_fails
        self.log.append(("comment", number, body))
        self.comments.append(_bot(body))
    def close(self, number):
        self.log.append(("close", number))
        self.faults = [f for f in self.faults if f["number"] != number]
    def edit_body(self, number, body):
        self.log.append(("edit", number))
        if getattr(self, "patch_fails", None):
            raise self.patch_fails
        self.issue = {**self.issue, "body": body}
    def fault_issues(self):
        return list(self.faults)
    def ensure_fault_label(self):
        self.log.append(("label",))
    def add_labels(self, number, labels):
        return [{"name": l} for l in labels]


def _bot(body, login="github-actions[bot]", edited=False):
    """A comment as GitHub returns it, by default red-main's own, unedited."""
    return {"body": body, "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "created_at": _z(NOW), "updated_at": _ago(-1) if edited else _z(NOW)}


def _active(created="2026-08-01T00:00:00.000-04:00"):
    return {"state": "active", "created_at": created}


def _ran(hours_ago):
    return {"created_at": _ago(hours_ago), "status": "completed"}


def _go(gh, root, event="workflow_run", env=ENV):
    return rma.run_schedules(gh, env, event=event, root=root, mirror_repo=MIRROR, now=NOW,
                             _smtp=FakeSMTP)


def _all_running(root):
    return {(w.file, w.repo): (_active(), _ran(2)) for w in rma.scheduled_workflows(root, MIRROR)}


# --- reading the schedules -------------------------------------------------

def test_only_workflows_with_a_schedule_are_watched_and_the_mirror_verify_too(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER,
                            "ci.yml": "on:\n  push:\n  # schedule: not here\n",
                            "commented.yml": 'on:\n  push:\n#    - cron: "0 6 * * *"\n'})
    got = rma.scheduled_workflows(root, MIRROR)
    assert [(w.key, w.repo, w.slots, w.error) for w in got] == [
        ("daily.yml", None, (737,), None),
        ("ladder.yml", None, (203, 443, 683), None),
        (f"verify.yml (on {MIRROR})", MIRROR, (737,), None)]


def test_a_cron_of_any_other_shape_is_unreadable_never_guessed(tmp_path):
    for cron in ("*/5 * * * *", "0 6 * * 1", "60 1 * * *", "0 24 * * *", "0 6 * *  *", "@daily"):
        assert rma.read_schedule(f'on:\n  schedule:\n    - cron: "{cron}"\n')[2], cron
    has, slots, error = rma.read_schedule("on:\n  schedule:\n  push:\n")
    assert has and slots == () and "no cron line" in error
    root = _tree(tmp_path, {"weekly.yml": 'on:\n  schedule:\n    - cron: "0 6 * * 1"\n'})
    gh = FakeGitHub(_all_running(root))
    assert _go(gh, root) == 1, "an unreadable schedule is the fault path"
    assert gh.issue is None and gh.faults
    assert "weekly.yml (its schedule: line 4 is not" in FakeSMTP.sent[0].get_content()
    assert ("info", "weekly.yml", None) not in gh.log, "an unread schedule is not judged"


def test_every_line_of_the_schedule_block_must_be_a_strict_cron_line():
    """The reader's example: a flow-style second entry must not be ignored
    while the first makes the schedule look understood."""
    codex = 'on:\n  schedule:\n    - cron: "0 6 * * *"\n    - {cron: "0 18 * * 1"}\n  push:\n'
    has, slots, error = rma.read_schedule(codex)
    assert has and slots == () and error == 'line 4 is not - cron: "M H * * *"'
    for text in ('on:\n  schedule: [{cron: "0 6 * * *"}]\n',            # flow style on the key
                 'on: {schedule: [{cron: "0 6 * * *"}], push: {}}\n',     # flow style on on:
                 'on: {"schedule": [{"cron": "0 6 * * *"}]}\n',
                 'on:\n  workflow_dispatch:\n    inputs:\n      schedule: {type: string}\n',  # loud, accepted
                 'on:\n  schedule:\n    - cron: 0 6 * * *\n',            # unquoted
                 'on:\n  schedule:\n    - cron: "0 6 * * *"\n      x: 1\n',
                 'on:\n  push:\n  x: {cron: "0 6 * * *"}\n',            # cron outside a block
                 'on:\n  schedule:\n    - cron: "0 6 * * *"\n  x: {cron: "0 7 * * *"}\n'):
        assert rma.read_schedule(text)[0] and rma.read_schedule(text)[2], text
    ok = ('on:\n  schedule:\n    # a comment\n\n    - cron: "0 6 * * *"   # why\n'
          "    - cron: '30 18 * * *'\n  workflow_dispatch:\n")
    assert rma.read_schedule(ok) == (True, (360, 1110), None)


def test_a_flow_style_schedule_is_watched_and_unreadable_never_unscheduled(tmp_path):
    root = _tree(tmp_path, {"flow.yml": 'on: {schedule: [{cron: "0 6 * * *"}]}\n',
                            "flow2.yml": 'on:\n  schedule: [{cron: "0 6 * * *"}]\n'})
    got = {w.key: w for w in rma.scheduled_workflows(root, MIRROR)}
    assert got["flow.yml"].error and got["flow2.yml"].error
    gh = FakeGitHub(_all_running(root))
    assert _go(gh, root) == 1 and gh.faults
    text = FakeSMTP.sent[0].get_content()
    assert "flow.yml (its schedule:" in text and "flow2.yml (its schedule:" in text


def test_a_missing_mirror_source_is_the_fault_path(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY}, verify=None)
    got = rma.scheduled_workflows(root, MIRROR)
    assert got[-1].error == "its source mirror/verify.yml is missing"
    gh = FakeGitHub(_all_running(root) | {("verify.yml", MIRROR): (_active(), _ran(1))})
    assert _go(gh, root) == 1 and gh.faults and gh.issue is None


def test_threshold_is_the_longest_slot_interval_plus_sixteen_hours():
    h = lambda t: t.total_seconds() / 3600
    assert h(rma.threshold((737,))) == 40                       # daily
    assert h(rma.threshold((203, 443, 683))) == 32              # ercot: 11:23 -> 03:23 is 16 h
    assert h(rma.threshold((675, 765, 1035))) == 34             # mirror-freshness: 17:15 -> 11:15
    assert h(rma.threshold((913, 1033, 1153, 1297))) == pytest.approx(33.6)   # jp: 21:37 -> 15:13
    assert rma.SCHEDULE_SLACK == timedelta(hours=16)


def test_the_real_scheduled_workflows_are_read_with_the_approved_thresholds():
    """Every scheduled workflow on this repository's main is understood, and
    the thresholds are the ones approved: 40 h daily, 32 h ercot, 34 h
    mirror-freshness, 33.6 h jp. A new scheduled workflow is watched with no
    list to update; a new cron shape fails here first."""
    private_file(".github/workflows/red-main.yml")
    got = {w.key: w for w in rma.scheduled_workflows(ROOT, "ArturoQuintana/talea")}
    assert all(w.error is None for w in got.values()), [w for w in got.values() if w.error]
    hours = {k: rma.threshold(w.slots).total_seconds() / 3600 for k, w in got.items()}
    assert hours == {"ercot.yml": 32, "jp.yml": pytest.approx(33.6), "liveness.yml": 40,
                     "mirror-freshness.yml": 34, "receipt-push-times.yml": 40, "red-main.yml": 40,
                     "stamp-margin.yml": 40, "write-gap.yml": 40,
                     "verify.yml (on ArturoQuintana/talea)": 40}
    assert "ci.yml" not in got


# --- judging one workflow ----------------------------------------------------

W = rma.Watched("daily.yml", "daily.yml", None, (737,), None)


def test_judge_running_just_inside_and_stopped_just_outside_the_threshold():
    assert rma.judge(W, _active(), _ran(39.9), NOW) is None
    why = rma.judge(W, _active(), _ran(40.1), NOW)
    assert why.startswith("no scheduled run for 40.1 h (threshold 40.0 h)")


def test_judge_any_state_but_active_is_stopped_at_once():
    for state in ("disabled_manually", "disabled_inactivity", "disabled_fork", "deleted"):
        assert rma.judge(W, {"state": state, "created_at": _ago(100)}, _ran(1), NOW) == \
            f"GitHub shows it as {state}"
    assert rma.judge(W, None, None, NOW) == "GitHub has no workflow for this file"


def test_judge_a_queued_run_counts_the_status_is_not_read():
    assert rma.judge(W, _active(), {"created_at": _ago(1), "status": "queued"}, NOW) is None


def test_judge_the_clock_starts_no_earlier_than_registration():
    new = _active(created=_ago(30))
    assert rma.judge(W, new, None, NOW) is None, "not judged before its first window"
    assert rma.judge(W, new, _ran(200), NOW) is None
    late = rma.judge(W, _active(created=_ago(41)), None, NOW)
    assert late.startswith("no scheduled run since GitHub registered it")


# --- telling it ----------------------------------------------------------------

def test_all_running_does_nothing(tmp_path, capsys):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    gh = FakeGitHub(_all_running(root))
    assert _go(gh, root) == 0
    assert [e for e in gh.log if e[0] != "info"] == [] and FakeSMTP.sent == []
    out = capsys.readouterr().out
    assert "[red-main] schedules: ladder.yml: running (newest scheduled run" in out
    assert "threshold 32.0 h" in out


def test_a_first_stop_opens_the_issue_and_emails_and_exits_zero(tmp_path):
    """Reported red is red-main WORKING: exit 0, so the heartbeat stays plain."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(33))
    flows[("verify.yml", MIRROR)] = ({"state": "disabled_inactivity", "created_at": _ago(900)}, _ran(1))
    gh = FakeGitHub(flows)
    assert _go(gh, root) == 0
    assert ("create", rma.SCHEDULES.title, "owner") in gh.log
    body = gh.issue["body"]
    assert body.endswith(f"Stopped: ladder.yml; verify.yml (on {MIRROR})")
    assert "GitHub shows it as disabled_inactivity" in body
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == f"talea scheduled workflow STOPPED: ladder.yml; verify.yml (on {MIRROR})"
    assert "https://example.test/issues/7" in msg.get_content()
    assert msg["Cc"] == ENV["ALERT_SMTP_USER"]


def test_while_stopped_only_the_daily_schedule_reminds(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(50),
             "html_url": "https://example.test/issues/3", "body": "x\n\nStopped: daily.yml"}
    gh = FakeGitHub(flows, issue=dict(issue))
    assert _go(gh, root, event="workflow_run") == 0
    assert [e for e in gh.log if e[0] != "info"] == [] and FakeSMTP.sent == []
    gh = FakeGitHub(flows, issue=dict(issue))
    assert _go(gh, root, event="schedule") == 0
    comment = [e for e in gh.log if e[0] == "comment"]
    assert len(comment) == 1 and comment[0][2].startswith("Still stopped — day 2:")
    assert FakeSMTP.sent[0]["Subject"] == "talea scheduled workflow still STOPPED — day 2: daily.yml"


def test_a_change_in_the_stopped_set_is_told_at_once_and_recorded(tmp_path):
    """A second workflow stopping while the issue is open must not hide behind it."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "x\n\nStopped: daily.yml"}
    gh = FakeGitHub(flows, issue=issue)
    assert _go(gh, root, event="workflow_run") == 0
    assert [e[0] for e in gh.log if e[0] != "info"] == ["comment", "edit"]
    assert gh.log[-2][2].startswith(rma.CHANGE_HEAD)
    assert gh.log[-2][2].endswith("Stopped: daily.yml; ladder.yml"), "the comment names the new set"
    assert gh.issue["body"].endswith("Stopped: daily.yml; ladder.yml")
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == ("talea scheduled workflows: the stopped set changed — now: "
                              "daily.yml; ladder.yml")
    assert "Before: daily.yml" in msg.get_content()


def test_one_running_again_while_another_is_stopped_is_a_change_too(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml; ladder.yml"}
    gh = FakeGitHub(flows, issue=issue)
    assert _go(gh, root, event="schedule") == 0
    assert [e[0] for e in gh.log if e[0] != "info"] == ["comment", "edit"], \
        "the change email replaces that run's reminder"
    assert len(FakeSMTP.sent) == 1 and "now: ladder.yml" in FakeSMTP.sent[0]["Subject"]


def test_a_failed_change_email_fails_the_run_and_is_not_retried(tmp_path):
    """No pending state: the lost email is reported by the heartbeat (/fail)."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    FakeSMTP.fail = True
    gh = FakeGitHub(flows, issue=issue)
    assert _go(gh, root) == 1
    FakeSMTP.fail = False
    assert _go(gh, root) == 0 and FakeSMTP.sent == []


def test_a_failing_email_over_three_runs_one_comment_one_attempt_per_change(tmp_path):
    """The change comment is posted only when the newest one's set differs, so
    a failing email cannot add a comment every run; each change is attempted
    once and fails its run (the heartbeat's /fail), and is not re-sent."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER, "late.yml": DAILY})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    FakeSMTP.fail = True
    gh = FakeGitHub(flows, issue=issue)
    assert [_go(gh, root) for _ in range(3)] == [1, 0, 0], "the same change: attempted once"
    assert len(gh.comments) == 1 and FakeSMTP.attempts == 1
    codes = []                                   # three different changes, one per run
    for stopped in (("daily.yml",), ("daily.yml", "late.yml"), ("late.yml",)):
        gh.flows = _all_running(root)
        for f in stopped:
            gh.flows[(f, None)] = (_active(), _ran(45))
        codes.append(_go(gh, root))
    assert codes == [1, 1, 1] and len(gh.comments) == 4 and FakeSMTP.attempts == 4


def test_a_change_with_no_email_settings_is_a_failure_not_an_acknowledgement(tmp_path, capsys):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    env = {k: v for k, v in ENV.items() if not k.startswith("ALERT_")}
    assert _go(FakeGitHub(flows, issue=issue), root, env=env) == 1
    assert "EMAIL-NOT-CONFIGURED" in capsys.readouterr().out


def test_a_body_that_never_updates_sends_the_change_email_once(tmp_path):
    """The reader's flood: email sent, PATCH failing on every run. The change
    comment is the record, so the same change is never emailed again."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    gh = FakeGitHub(flows, issue=issue)
    gh.patch_fails = RuntimeError("PATCH refused")
    codes = [_go(gh, root) for _ in range(4)]
    assert codes == [1, 0, 0, 0], "the failed PATCH fails its run only"
    assert len(FakeSMTP.sent) == 1 and gh.issue["body"] == "Stopped: daily.yml"
    flows[("daily.yml", None)] = (_active(), _ran(41))      # and a later real change is still told
    assert _go(gh, root) == 1 and len(FakeSMTP.sent) == 2
    assert "now: daily.yml; ladder.yml" in FakeSMTP.sent[1]["Subject"]


def test_the_set_returning_to_the_bodys_is_still_a_change(tmp_path):
    """The newest change comment is the set last told, not the body."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    told = _bot(f"{rma.CHANGE_HEAD} Stopped now:\n...\n\nStopped: daily.yml; ladder.yml")
    noise = _bot("Stopped: ladder.yml (a person's comment, not a change record)", login="someone")
    gh = FakeGitHub(flows, issue=issue, comments=[told, noise])
    assert _go(gh, root) == 0
    assert len(FakeSMTP.sent) == 1 and "Before: daily.yml; ladder.yml" in FakeSMTP.sent[0].get_content()


def test_missing_email_settings_fail_the_opening_reminder_and_recovery(tmp_path, capsys):
    """Every email of this target is required: "skipped" would exit 0 and the
    heartbeat would stay plain while nothing was told."""
    root = _tree(tmp_path, {"daily.yml": DAILY})
    stopped = _all_running(root)
    stopped[("daily.yml", None)] = (_active(), _ran(50))
    env = {k: v for k, v in ENV.items() if not k.startswith("ALERT_")}
    gh = FakeGitHub(stopped)
    assert _go(gh, root, env=env) == 1 and gh.issue is not None                 # opening
    assert _go(gh, root, event="schedule", env=env) == 1                        # reminder
    gh.flows = _all_running(root)
    assert _go(gh, root, env=env) == 1 and ("close", 7) in gh.log               # recovery
    assert capsys.readouterr().out.count("EMAIL-NOT-CONFIGURED") == 3


def test_a_look_alike_or_edited_record_cannot_suppress_a_change(tmp_path):
    """The reader's case: the recorded set is daily.yml, ladder.yml then stops,
    and a person's comment, or an edited red-main record, already names the
    new set. Neither is a record: the change is still told."""
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    fake = f"{rma.CHANGE_HEAD}\n\nStopped: daily.yml; ladder.yml"
    for disguise in (_bot(fake, login="a-person"), _bot(fake, edited=True),
                     _bot(fake, login="github-actions"),
                     _bot("Stopped: daily.yml; ladder.yml")):          # red-main's, but no heading
        issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
                 "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
        FakeSMTP.sent = []
        gh = FakeGitHub(flows, issue=issue, comments=[disguise])
        assert _go(gh, root) == 0, disguise
        assert len(FakeSMTP.sent) == 1 and "Before: daily.yml\n" in FakeSMTP.sent[0].get_content()
    assert rma._record(_bot(fake)) and not rma._record({"body": fake})


def test_a_failed_change_comment_sends_no_email_and_fails_the_run(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("ladder.yml", None)] = (_active(), _ran(40))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    gh = FakeGitHub(flows, issue=issue)
    gh.comment_fails = RuntimeError("comment refused")
    assert _go(gh, root) == 1
    assert FakeSMTP.sent == [] and ("edit", 3) not in gh.log


def test_failed_opening_reminder_and_recovery_emails_each_fail_the_run(tmp_path):
    """Every lost email of this target fails the run, so the heartbeat posts /fail."""
    root = _tree(tmp_path, {"daily.yml": DAILY})
    stopped = _all_running(root)
    stopped[("daily.yml", None)] = (_active(), _ran(50))
    FakeSMTP.fail = True
    gh = FakeGitHub(stopped)
    assert _go(gh, root) == 1 and gh.issue is not None                 # opening
    assert _go(gh, root, event="schedule") == 1                        # reminder
    gh.flows = _all_running(root)
    assert _go(gh, root) == 1 and ("close", 7) in gh.log               # recovery


def test_an_issue_whose_stopped_line_was_removed_is_told_again(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "edited by a person"}
    gh = FakeGitHub(flows, issue=issue)
    assert _go(gh, root) == 0
    assert "Before: unknown" in FakeSMTP.sent[0].get_content()
    assert gh.issue["body"].endswith("Stopped: daily.yml")


def test_all_running_again_closes_and_emails(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    issue = {"number": 3, "title": rma.SCHEDULES.title, "created_at": _ago(10),
             "html_url": "https://example.test/issues/3", "body": "Stopped: daily.yml"}
    gh = FakeGitHub(_all_running(root), issue=issue)
    assert _go(gh, root) == 0
    assert [e[0] for e in gh.log if e[0] != "info"] == ["comment", "close"]
    assert FakeSMTP.sent[0]["Subject"] == "talea scheduled workflows: all running again"


def test_red_mains_own_schedule_is_watched_from_a_workflow_run_event(tmp_path):
    """red-main runs after every CI run too: its dropped schedule is seen in band."""
    root = _tree(tmp_path, {"red-main.yml": DAILY})
    flows = _all_running(root)
    flows[("red-main.yml", None)] = (_active(), _ran(41))
    gh = FakeGitHub(flows)
    assert _go(gh, root, event="workflow_run") == 0
    assert gh.issue["body"].endswith("Stopped: red-main.yml")


# --- what cannot be read is a fault, never "running" --------------------------

def test_an_unreadable_workflow_is_the_fault_path_and_nothing_else_is_judged(tmp_path, capsys):
    root = _tree(tmp_path, {"daily.yml": DAILY, "ladder.yml": LADDER})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))           # a real stop ...
    flows[("ladder.yml", None)] = urllib.error.URLError("down")  # ... and an unreadable one
    gh = FakeGitHub(flows)
    assert _go(gh, root) == 1
    assert gh.issue is None, "a partial read never opens, changes or closes the stop issue"
    assert gh.faults and gh.faults[0]["title"] == rma.history_title(rma.SCHEDULES)
    assert rma.FAULT_LABEL in gh.faults[0]["labels"]
    assert "the schedule, state or scheduled runs of ladder.yml (URLError)" in \
        FakeSMTP.sent[0].get_content()


def test_an_unreadable_issue_list_is_the_fault_path(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    gh = FakeGitHub(_all_running(root))
    gh.issues_fail = rma.HistoryCut("the issues")
    assert _go(gh, root) == 1
    assert gh.faults and FakeSMTP.sent[0]["Subject"].startswith(
        "talea red-main could not read enough history for scheduled workflows")


def test_a_fault_issue_closes_when_everything_reads_again(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    fault = {"number": 5, "title": rma.history_title(rma.SCHEDULES), "labels": [rma.FAULT_LABEL],
             "html_url": "https://example.test/issues/5"}
    gh = FakeGitHub(_all_running(root), faults=[fault])
    assert _go(gh, root) == 0
    assert ("close", 5) in gh.log and gh.faults == []


def test_an_alert_that_crashes_fails_the_run(tmp_path):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    flows = _all_running(root)
    flows[("daily.yml", None)] = (_active(), _ran(50))
    gh = FakeGitHub(flows)
    gh.create_issue = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _go(gh, root) == 1


# --- the GitHub reads ----------------------------------------------------------

def _opener(answers, seen):
    def _open(req, timeout=None):
        seen.append((req.full_url, "Authorization" in req.headers))
        code, body = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return io.BytesIO(json.dumps(body).encode())
    return _open


def test_reads_own_repo_404_is_no_workflow_other_errors_raise():
    seen = []
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(404, None)], seen))
    assert gh.workflow_info("gone.yml") is None
    assert seen == [("https://api.test/repos/o/r/actions/workflows/gone.yml", True)]
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(500, None)], []))
    with pytest.raises(urllib.error.HTTPError):
        gh.workflow_info("x.yml")


def test_reads_on_the_mirror_retry_without_the_token_when_refused():
    seen = []
    runs = {"workflow_runs": [{"created_at": "2026-11-17T06:40:00Z"}]}
    gh = rma.GitHub("o/r", "tok", api="https://api.test",
                    _open=_opener([(403, None), (200, runs)], seen))
    assert gh.newest_scheduled_run("verify.yml", MIRROR) == runs["workflow_runs"][0]
    assert seen[0] == (f"https://api.test/repos/{MIRROR}/actions/workflows/verify.yml/runs"
                       "?event=schedule&per_page=1", True)
    assert seen[1][1] is False
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(200, {"workflow_runs": []})], []))
    assert gh.newest_scheduled_run("x.yml") is None
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(404, None)], []))
    with pytest.raises(urllib.error.HTTPError):
        gh.newest_scheduled_run("x.yml"), "a run list that is not there is unreadable, not empty"


def test_target_is_selectable_and_needs_the_mirror(monkeypatch):
    seen = {}
    def fake(gh, env, *, event, root, mirror_repo, now=None, _smtp=None):
        seen.update(event=event, root=root, mirror_repo=mirror_repo)
        return 0
    monkeypatch.setattr(rma, "run_schedules", fake)
    for k, v in (("GITHUB_REPOSITORY", "o/r"), ("GITHUB_TOKEN", "tok"), ("GITHUB_EVENT_NAME", "schedule")):
        monkeypatch.setenv(k, v)
    assert rma.main(["--target", "schedules", "--repo", MIRROR]) == 0
    assert seen == {"event": "schedule", "root": ROOT, "mirror_repo": MIRROR}
    with pytest.raises(SystemExit):
        rma.main(["--target", "schedules"])
    with pytest.raises(SystemExit):
        rma.main(["--target", "schedules", "--repo", MIRROR, "--drill"])


# --- the workflow ----------------------------------------------------------------

def _red_main() -> str:
    return private_file(".github/workflows/red-main.yml").read_text()


def test_red_main_runs_the_schedules_step_guarded_like_the_others():
    text = _red_main()
    step = text.split("- name: Report scheduled workflows that stopped running (issue + email)", 1)[1]
    step = step.split("- name:", 1)[0]
    assert "if: ${{ always() && !inputs.drill }}" in step
    assert ("run: uv run --no-project python scripts/red_main_alert.py --target schedules "
            "--repo ArturoQuintana/talea") in step
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in step


def _heartbeat_script() -> tuple[str, str]:
    text = _red_main()
    step = text.split("- name: Ping red-main's heartbeat (healthchecks.io), if configured", 1)[1]
    assert "- name:" not in step, "the heartbeat must be the last step"
    head, script = step.split("run: |\n", 1)
    return head, "\n".join(l[10:] for l in script.splitlines())


def test_the_heartbeat_step_is_last_runs_always_and_reads_red_mains_own_outcome():
    head, _ = _heartbeat_script()
    assert "if: ${{ always() }}" in head
    assert "HEARTBEAT_URL: ${{ secrets.RED_MAIN_HEARTBEAT_URL }}" in head
    assert "JOB_STATUS: ${{ job.status }}" in head
    assert "REF: ${{ github.ref }}" in head
    assert "DRILL: ${{ inputs.drill == true }}" in head


def test_heartbeat_only_a_monitoring_run_on_main_pings_at_all(tmp_path):
    """A drill skips the eight targets and another ref does not watch main:
    neither may reset the check, and neither posts /fail."""
    for ref, drill in (("refs/heads/other", "false"), ("refs/heads/main", "true"),
                       ("refs/tags/v0", "false"), ("", "false")):
        for status in ("success", "failure"):
            r, calls = _ping(tmp_path, "https://hc.test/abc", status, ref=ref, drill=drill)
            assert r.returncode == 0 and calls == "", (ref, drill, status)
            assert "not a monitoring run on main" in r.stdout


def _ping(tmp_path, url, status, curl_exit=0, ref="refs/heads/main", drill="false"):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    (bin_ / "curl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit {curl_exit}\n')
    (bin_ / "curl").chmod(0o755)
    _, script = _heartbeat_script()
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "JOB_STATUS": status, "REF": ref, "DRILL": drill}
    if url is not None:
        env["HEARTBEAT_URL"] = url
    r = subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env,
                       capture_output=True, text=True)
    return r, (log.read_text() if log.exists() else "")


def test_heartbeat_a_reported_red_pings_plain_and_a_malfunction_posts_fail(tmp_path):
    """The heartbeat means "red-main itself worked", never "main is green": a
    red target red-main reported exits 0 (the job is a success: plain ping);
    only a failed step (a read it could not do, a failed email, a crash) makes
    it /fail."""
    r, calls = _ping(tmp_path, "https://hc.test/abc", "success")
    assert r.returncode == 0 and calls.split()[-1] == "https://hc.test/abc"
    assert "[red-main] heartbeat: pinged (red-main success)" in r.stdout
    (tmp_path / "curl.log").unlink()
    r, calls = _ping(tmp_path, "https://hc.test/abc", "failure")
    assert r.returncode == 0 and calls.split()[-1] == "https://hc.test/abc/fail"
    assert "https://hc.test" not in r.stdout, "the URL is never printed"


def test_heartbeat_url_with_a_trailing_slash(tmp_path):
    r, calls = _ping(tmp_path, "https://hc.test/abc/", "failure")
    assert calls.split()[-1] == "https://hc.test/abc/fail"
    (tmp_path / "curl.log").unlink()
    r, calls = _ping(tmp_path, "https://hc.test/abc/", "success")
    assert calls.split()[-1] == "https://hc.test/abc"


def test_heartbeat_without_the_secret_is_a_noop_that_says_so(tmp_path):
    for url in (None, ""):
        r, calls = _ping(tmp_path, url, "success")
        assert r.returncode == 0 and calls == ""
        assert r.stdout.strip() == "[red-main] heartbeat: RED_MAIN_HEARTBEAT_URL is not set; no ping"


def test_a_failed_ping_never_fails_the_run(tmp_path):
    r, _ = _ping(tmp_path, "https://hc.test/abc", "success", curl_exit=7)
    assert r.returncode == 0
    assert "the ping failed; healthchecks.io alarms on the missing ping" in r.stdout


def test_reported_red_is_exit_zero_on_every_ordinary_target():
    """The other half of the heartbeat rule, on the targets that judge runs: an
    ordinary red that was reported (issue filed, email sent) exits 0."""
    class GH:
        def __init__(self):
            self.made = None
        def workflow_runs(self, *a, **k):
            return [{"conclusion": "failure", "created_at": "2026-11-17T11:00:00Z",
                     "head_sha": "a" * 40, "html_url": "https://example.test/runs/1"},
                    {"conclusion": "success", "created_at": "2026-11-17T09:00:00Z",
                     "head_sha": "b" * 40, "html_url": "https://example.test/runs/0"}]
        def open_issue(self, title):
            return None
        def issues_since(self, title, since):
            return []
        def create_issue(self, title, body, owner, labels=None):
            self.made = {"number": 1, "html_url": "https://example.test/issues/1"}
            return self.made
        def fault_issues(self):
            return []
    for key, target in rma.TARGETS.items():
        if target is rma.SCHEDULES:
            continue
        FakeSMTP.sent = []
        gh = GH()
        assert rma.run(gh, ENV, event="workflow_run", now=NOW, _smtp=FakeSMTP, target=target) == 0, key
        assert gh.made and len(FakeSMTP.sent) == 1, key
    FakeSMTP.fail = True
    assert rma.run(GH(), ENV, event="workflow_run", now=NOW, _smtp=FakeSMTP, target=rma.CI) == 1, \
        "a failed email is red-main failing"


def test_no_address_or_credential_in_the_new_code():
    text = (ROOT / "scripts" / "red_main_alert.py").read_text()
    assert not re.search(r"[\w.+-]+@[\w-]+\.(com|net|org|io)\b", text)
    assert "hc-ping.com/" not in text and "hc-ping.com/" not in _red_main()


def test_a_workflow_gone_from_the_mirror_is_none_other_refusals_raise():
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(403, None), (404, None)], []))
    assert gh.workflow_info("verify.yml", MIRROR) is None
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(403, None), (500, None)], []))
    with pytest.raises(urllib.error.HTTPError):
        gh.workflow_info("verify.yml", MIRROR)


def test_edit_body_patches_the_issue():
    seen = []
    def _open(req, timeout=None):
        seen.append((req.get_method(), req.full_url, json.loads(req.data)))
        return io.BytesIO(b"{}")
    rma.GitHub("o/r", "tok", api="https://api.test", _open=_open).edit_body(3, "Stopped: x.yml")
    assert seen == [("PATCH", "https://api.test/repos/o/r/issues/3", {"body": "Stopped: x.yml"})]


def test_a_fault_path_that_itself_fails_still_fails_the_run(tmp_path, capsys):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    flows = _all_running(root)
    flows[("daily.yml", None)] = urllib.error.URLError("down")
    gh = FakeGitHub(flows)
    gh.create_issue = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _go(gh, root) == 1
    assert "[red-main] the fault could not be handled (RuntimeError)" in capsys.readouterr().out


def test_a_fault_issue_that_cannot_be_closed_fails_the_run(tmp_path, capsys):
    root = _tree(tmp_path, {"daily.yml": DAILY})
    fault = {"number": 5, "title": rma.history_title(rma.SCHEDULES), "labels": [rma.FAULT_LABEL],
             "html_url": "https://example.test/issues/5"}
    gh = FakeGitHub(_all_running(root), faults=[fault])
    gh.comment = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _go(gh, root) == 1
    assert "[red-main] the fault issue could not be handled (RuntimeError)" in capsys.readouterr().out


def test_issue_comments_are_read_to_the_end_and_a_cut_listing_raises():
    page = [{"body": str(i)} for i in range(100)]
    seen = []
    gh = rma.GitHub("o/r", "tok", api="https://api.test",
                    _open=_opener([(200, page), (200, [{"body": "last"}])], seen))
    got = gh.issue_comments(3)
    assert len(got) == 101 and got[-1] == {"body": "last"}
    assert seen[1][0] == "https://api.test/repos/o/r/issues/3/comments?per_page=100&page=2"
    gh = rma.GitHub("o/r", "tok", api="https://api.test", _open=_opener([(200, page)] * 2, []))
    with pytest.raises(rma.HistoryCut):
        gh.issue_comments(3, max_pages=2)
