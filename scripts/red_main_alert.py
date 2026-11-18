#!/usr/bin/env python3
"""Tell the operator when CI on main is red — and keep saying it until it is green.

Incident 2026-11-10: CI on main was red for 32 days (2026-10-09 -> 11-10) and
nothing told anyone. Most pushes to main come from the server's deploy key,
which is not a GitHub user, so GitHub's own "run failed" email had nobody to go
to; four resolver PRs sat behind the red check meanwhile.

Two channels, both on every state change, one repeating:
  1. a GitHub issue "CI is red on main", assigned to the repo owner (so GitHub
     notifies them whatever their watch settings), commented once a day while
     main stays red, closed when main is green again;
  2. an email from the project's alert account to the operator, copied to the
     alert account itself — the one mailbox the project's agents can read.

Run by .github/workflows/red-main.yml: after every CI run on main, once a day,
and on demand (--drill proves both channels end to end without a real red).

A SECOND TARGET (2026-11-11): the public mirror's `verify` run — the job behind
the README badge, which re-derives the whole ledger on GitHub. Its failure had
the same gap: the mirror is pushed by a deploy key, so GitHub emails nobody. The
weekly auditor was meant to read that run's status but cannot reach the API from
its sandbox (audits of 2026-11-02 and 11-09), and it must not be given
credentials. `--target mirror-verify --repo OWNER/MIRROR` reads the mirror's runs
and files the issue HERE, in the repo this workflow runs in. Same rules: first
red opens and emails, the daily schedule repeats, first green closes.

A THIRD TARGET (2026-11-13): this repo's own `origin-liveness` run
(.github/workflows/liveness.yml), which fails when a registered market stops
reaching origin — the class the server cannot see from its own clone (incident
2026-11-03..09). It is a scheduled run, so again GitHub emails nobody.
`--target liveness` reports it; same rules. Later targets, on the same rules:
`--target freshness` (the public mirror's freshness, register T-28) and
`--target push-times` (when each receipt reached GitHub, register T-35:
.github/workflows/receipt-push-times.yml).

A FAILURE A NEWER GREEN RUN HID (register T-38, 2026-11-16): the state is the
newest decisive run, so a failed run F followed by a green run G before F's
red-main job read GitHub opened nothing and emailed nobody. And red-main's
concurrency group keeps one pending job, so F's job may never run at all. Two
things close it. On a workflow_run event the triggering run (from the event
payload) is added to the list if GitHub's listing does not show it yet. And on
every run, whatever the event: each red streak of the last LOOKBACK_DAYS that
a success has already ended is reported late, oldest first, unless it was
covered. Covered: an issue with the target's title was open at some instant
between the streak's first failure and the success that ended it (created at
or before that success, and not closed before that failure), or an issue
names one of its runs by its exact URL (a late report names its runs). A
late report opens the issue and sends the red email; then it closes the
issue at the success and sends the green email. If the red email fails, it
stops there: the issue stays open (the ordinary path closes it later), no
later streak is reported in that run, and the run fails. So a missed failure
is reported once, in order, by the next red-main run of any kind (the daily
schedule at the latest), for every target, polled ones included. The run
history is read page by page until it covers the lookback and starts at a
success (at most MAX_PAGES pages); the issues are read page by page. A read
that reaches its page cap first is never taken as complete, even when nothing
decisive was read: it is told through its own issue, "red-main could not read
enough history for <target>", labelled FAULT_LABEL and found by listing the
open issues with that label to the end (a short list of its own, not the
listing that was cut; if it cannot be read, nothing is done about the fault but
the run fails). The label is confirmed before a fault issue is opened (read; created
if missing); if it cannot be, nothing is opened or emailed and the run fails. An
issue GitHub returns without the label gets it added; if that fails, the issue is
closed when GitHub accepts the close (and only then commented, saying so), with
no email, and the run fails. An issue returned without a number cannot be
labelled or closed: no email, and the run fails. Opened with one email the first
time; only the daily schedule
comments and reminds while it lasts; closed with one email ("history readable
again") when a later run reads enough. Late reports are skipped while it lasts
and every run fails. ORDER: the target's own alert first whenever its open
issue can be known, then the fault path, each guarded so a failure of one
never suppresses the other. At most
MAX_LATE streaks are reported one by one in a run (the oldest); any more go
into ONE closed issue naming all their runs and ONE summary email, so a
flapping workflow cannot send dozens of emails (the summary email goes
before its issue is closed; if it fails the issue stays open). A drill's red run (the
freshness check's drill=true run fails on purpose) is a red streak like any
other: if its alert was lost, it is reported late too, which is what a drill
is for. STATED LIMITS: a run re-run on main to a different conclusion replaces
the failed attempt in GitHub's listing (only a person re-runs on main, and
they saw it); an issue created whose API response was lost is not known to
have been created; a report half finished when a run stops is not resumed
(the open issue it leaves is the record, and the ordinary path closes it,
with the green email, at the next run); an issue left open across a green
whose red-main job was cancelled or lost also covers a later red streak (a
consequence of the open-interval rule: that streak is not told late, but the
issue stays open and the daily reminder goes on); a failed FIRST email about
a history fault is not retried (the run fails, and the daily reminder follows
while the fault lasts); a person removing the label from an open fault issue
leads to at most one replacement issue, and the old one stays open: the script
never closes it (it no longer finds it); if GitHub kept dropping the label, every
run would open a fault issue and close it when GitHub accepts the close (with no
email); if GitHub also refuses the close, or returns an issue without a number,
the unlabelled fault issue may stay open, one per run while the cut lasts; nothing
is emailed; each run fails, so the red run is the signal.

A SCHEDULED WORKFLOW THAT STOPS RUNNING (`--target schedules`, 2026-11-17):
every target above judges runs that exist, so a schedule GitHub disabled,
dropped or never registered turns nothing red. This target reads every
workflow on the checked-out main that has a `schedule:` block, plus the public
mirror's verify run (its source is mirror/verify.yml; `--repo` names the
mirror). Its schedule must be a `schedule:` block whose every line is
`- cron: "M H * * *"` (or a comment); anything else makes it unreadable (the
fault path), never a guess. A workflow is STOPPED
when GitHub shows it in any state but `active`, has no workflow for its file,
or when its newest scheduled run (by created_at, in any status: a queued run
means the schedule fired) is older than its THRESHOLD: the longest interval
between its cron slots plus SCHEDULE_SLACK (16 h; GitHub starts our scheduled
runs 4 to 10 hours late). The clock starts no earlier than the time GitHub
registered the workflow, so a new one is not judged before its first window.
One issue, "A scheduled workflow has stopped running", lists every stopped
workflow; its body ends with the line "Stopped: <names>", the set last told.
First stop: open and email. While it lasts: the daily schedule comments and
reminds. The SET changes while the issue is open (another one stops, or one
runs again while others are still stopped): first a comment ending with the
new "Stopped:" line (the record), then an email, then the body. The four
emails of this target's own alert (opening, reminder, change, all running
again) must go: missing email settings count as a failure (exit 1); the fault
path's emails follow that path's own rule, as for every target. The set last told is the newest
such comment's, else the body's, so one change is emailed once even if the
body never updates; a failed comment sends no email. All running again: close
and email. A schedule, state, run list, issue or comment list that cannot be
read is a fault (the `red-main-fault` issue above), and nothing else is judged
in that run: a partial read is never taken as all running. STATED LIMITS: a
lost email (any of those four) is not retried, no pending state is kept;
the run fails, so red-main's heartbeat posts /fail, which is the backup
channel. Only red-main's own unedited comments are records: a person's
look-alike comment, or an edited record, is ignored. The discovery reads the
workflow source as written: a schedule deliberately disguised (escaped keys
and the like) is not detected; accidental shapes are what it guards against.
A cron edited on an existing workflow does not restart the clock; the
first gap under the new schedule may be up to a day longer than the threshold
allows, and the false alarm closes itself at the next scheduled run.

Stdlib-only, like verify_ledger.py. No address or credential lives in this file:
ALERT_SMTP_USER / ALERT_SMTP_PASSWORD / ALERT_EMAIL_TO come from the workflow's
secrets. Without them the email is skipped (and said so) — the issue still files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import NamedTuple


class Target(NamedTuple):
    """One watched workflow. `name`/`where` build the subjects ("talea CI RED on
    main"); `subject` starts the sentences; `label` is the log word; `why` is
    the consequence line of the daily reminder."""
    workflow: str
    title: str
    name: str
    where: str
    subject: str
    label: str
    why: str


CI = Target("ci.yml", "CI is red on main", "CI", "on main", "CI on main", "main",
            "While main is red, no resolver PR can merge.")
MIRROR_VERIFY = Target(
    "verify.yml", "Public verify is red on the mirror", "public verify",
    "on the mirror", "The public mirror's verify run", "mirror verify",
    "While it is red, the published ledger does not re-derive on GitHub and the "
    "README badge shows failing.")
LIVENESS = Target(
    "liveness.yml", "A market has stopped reaching origin", "origin liveness",
    "on origin", "The origin liveness check", "origin liveness",
    "While it is red, at least one registered market has committed nothing to "
    "origin for two days or its newest stored price is more than two days old; "
    "the run log names it. If the server's own digest shows that market as "
    "fresh, the server's clone has diverged from origin (INFRA.md runbook).")
FRESHNESS = Target(
    "mirror-freshness.yml", "The public mirror's freshness check is red",
    "mirror freshness", "on the mirror", "The mirror-freshness check", "mirror freshness",
    "Red can mean any of these; the run log is the authority on which. STALE: the "
    "public mirror has missed two publish windows in a row, so what readers see is "
    "behind the ledger (the publisher's reason, if it refused or its push failed, is "
    "in the server's journal). CANNOT JUDGE: the check could not determine freshness "
    "(for example, GitHub's push history was unreadable or could not be shown complete, "
    "or the time-zone data was missing). A failed step before the check (checkout, uv): "
    "the check did not run. DRILL: a run started with drill=true fails on purpose; it "
    "says nothing about the mirror. The next successful run of the check closes this issue.")
PUSH_TIMES = Target(
    "receipt-push-times.yml", "The receipt push-time check is red",
    "receipt push times", "on origin", "The receipt push-time check", "receipt push times",
    "Red can mean either; the run log is the authority. UNVERIFIABLE (exit 1, trigger T3): "
    "a receipt GitHub received at or after its market's deadline, on or after the start date. "
    "Its stamp may be honest, but GitHub cannot vouch that it existed before the gate; this is "
    "not a leak finding. CANNOT JUDGE (exit 2, trigger T4): the check could not judge: a commit "
    "with no push in the record, a push history unreadable or not reaching back, a partial or "
    "shallow clone, or a reset main. A failed step before the check: the check did not run. "
    "The next successful run closes this issue.")
SCHEDULES = Target(
    "(every scheduled workflow)", "A scheduled workflow has stopped running",
    "scheduled workflows", "", "A scheduled workflow", "schedules",
    "While it lasts, what that workflow watches or writes is not being done on "
    "schedule; a writer's market misses its receipts, a detector's class goes "
    "unwatched. The issue lists each stopped workflow and why.")
TARGETS = {"ci": CI, "mirror-verify": MIRROR_VERIFY, "liveness": LIVENESS, "freshness": FRESHNESS,
           "push-times": PUSH_TIMES, "schedules": SCHEDULES}

TITLE = CI.title
DRILL_TITLE = "[DRILL] CI is red on main — delivery test"
RED = {"failure", "timed_out", "startup_failure"}
DECISIVE = RED | {"success"}       # cancelled / skipped runs say nothing either way
LOOKBACK_DAYS = 7                  # how far back an ended, unreported red streak is looked for
MAX_LATE = 3                       # streaks reported late one by one per run; the rest in one summary
MAX_PAGES = 10                     # pages of 100 runs read at most per target
PER_PAGE = 100
FAULT_LABEL = "red-main-fault"     # labels the "could not read enough history" issues
SCHEDULE_SLACK = timedelta(hours=16)   # above GitHub's observed lateness swing (up to ~10 h)


# --- pure decisions ---------------------------------------------------------

def main_state(runs: list[dict]) -> dict | None:
    """Summarise the newest-first list of completed CI runs on main.

    None when no run is decisive. Otherwise {"red": bool, "latest": run} plus,
    when red, "first_red" (the oldest failure since the last success) and
    "failed_runs" (how many in that streak)."""
    decisive = [r for r in runs if r.get("conclusion") in DECISIVE]
    if not decisive:
        return None
    latest = decisive[0]
    if latest["conclusion"] == "success":
        return {"red": False, "latest": latest}
    streak = []
    for r in decisive:
        if r["conclusion"] == "success":
            break
        streak.append(r)
    return {"red": True, "latest": latest, "first_red": streak[-1],
            "failed_runs": len(streak)}


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def with_trigger(runs: list[dict], trigger: dict | None) -> list[dict]:
    """The listed runs, newest first, with the run that triggered this job
    added if it is decisive and GitHub's listing does not show it yet."""
    if (not trigger or trigger.get("conclusion") not in DECISIVE
            or any(r.get("html_url") == trigger.get("html_url") for r in runs)):
        return runs
    return sorted(runs + [trigger], key=lambda r: _at(r["created_at"]), reverse=True)


URL = re.compile(r"https?://[^\s()<>`'\"]+")


def named_urls(text: str) -> set[str]:
    """The exact URLs written in `text` (trailing punctuation dropped)."""
    return {u.rstrip(".,;:") for u in URL.findall(text or "")}


def _finished(run: dict) -> datetime:
    return _at(run.get("updated_at") or run["created_at"])


def covered(streak: list[dict], ended_by: dict, issues: list[dict]) -> bool:
    """Was this streak already told? Yes if an issue (of this target's title)
    was open at some instant between its first failure and the success that
    ended it, or if an issue names one of its runs by its exact URL."""
    first, end = min(_at(r["created_at"]) for r in streak), _finished(ended_by)   # any order
    urls = {r["html_url"] for r in streak}
    for i in issues:
        closed = i.get("closed_at")
        if _at(i["created_at"]) <= end and (closed is None or _at(closed) >= first):
            return True
        if urls & named_urls(i.get("body") or ""):
            return True
    return False


def missed_streaks(runs: list[dict], issues: list[dict], since: datetime) -> list[tuple[list[dict], dict]]:
    """Red streaks that a success has already ended, with a red run created
    at or after `since`, that no issue covers. Oldest first; each is (its red
    runs, newest first; the success that ended it)."""
    decisive = [r for r in runs if r.get("conclusion") in DECISIVE]
    out, streak = [], []
    for r in reversed(decisive):                  # oldest to newest
        if r["conclusion"] != "success":
            streak.append(r)
            continue
        if (streak and any(_at(x["created_at"]) >= since for x in streak)
                and not covered(streak, r, issues)):
            out.append((list(reversed(streak)), r))
        streak = []
    return out


def enough_history(runs: list[dict], since: datetime) -> bool:
    """True once the runs read (newest first) reach back before `since` and
    the oldest decisive one read is a success: every streak read is then
    complete."""
    decisive = [r for r in runs if r.get("conclusion") in DECISIVE]
    return bool(runs) and _at(runs[-1]["created_at"]) < since and bool(decisive) \
        and decisive[-1]["conclusion"] == "success"


def decide(*, red: bool, issue_open: bool, event: str) -> list[str]:
    """What to do, in order. The first red opens the issue and emails at once;
    while red, only the DAILY schedule repeats (one reminder a day, not one per
    push — main takes several pushes a day); the first green closes and emails."""
    if red and not issue_open:
        return ["open", "email_red"]
    if red and event == "schedule":
        return ["comment", "email_still_red"]
    if not red and issue_open:
        return ["close", "email_green"]
    return []


def days_red(first_red_at: str, now: datetime) -> int:
    start = datetime.fromisoformat(first_red_at.replace("Z", "+00:00"))
    return max(0, (now - start).days)


def build_email(kind: str, facts: dict, *, sender: str, to: str,
                target: Target = CI) -> EmailMessage:
    """The action wanted comes first; the alert account is always copied."""
    sha = facts.get("sha", "")[:7]
    t = target
    subjects = {
        "email_red": f"talea {t.name} RED {t.where} — {sha}",
        "email_still_red": f"talea {t.name} still RED {t.where} — day {facts.get('days', 0)}",
        "email_green": f"talea {t.name} green again {t.where} — {sha}",
        "drill": "[DRILL] talea red-main alert — delivery test",
        "email_late_summary": f"talea {t.name}: {facts.get('streaks', 0)} more red streaks "
                              f"{t.where} were never reported",
        "email_history_cut": f"talea red-main could not read enough history for {t.name}",
        "email_history_still": f"talea red-main still cannot read enough history for {t.name}",
        "email_history_ok": f"talea red-main reads enough history again for {t.name}",
        "email_stopped": f"talea scheduled workflow STOPPED: {facts.get('names', '')}",
        "email_still_stopped": f"talea scheduled workflow still STOPPED — day {facts.get('days', 0)}: "
                               f"{facts.get('names', '')}",
        "email_stopped_changed": f"talea scheduled workflows: the stopped set changed — now: "
                                 f"{facts.get('names', '')}",
        "email_running_again": "talea scheduled workflows: all running again",
    }
    bodies = {
        "email_red": (
            f"{t.subject} is failing. Open the failing run and fix or escalate:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            f"First red run: {facts.get('first_red_at', '')} (commit {facts.get('first_red_sha', '')[:7]}).\n"
            + ("Found late: a newer run was already green when this was read; the next "
               "email says so and closes the issue." if facts.get("late") else
               "A reminder follows once a day until it is green.")),
        "email_still_red": (
            f"{t.subject} is STILL failing — day {facts.get('days', 0)}, "
            f"{facts.get('failed_runs', 0)} failed runs. Open the latest failing run:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            f"Red since: {facts.get('first_red_at', '')}.\n"
            f"{t.why}"),
        "email_green": (
            f"{t.subject} is green again. Nothing to do.\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Closed: {facts.get('issue_url', '')}"),
        "email_late_summary": (
            f"{facts.get('streaks', 0)} more red streaks of {t.subject} were never reported "
            f"when they happened; each ended green. The oldest began at "
            f"{facts.get('first_red_at', '')}:\n  {facts.get('run_url', '')}\n\n"
            f"Every failed run is listed in: {facts.get('issue_url', '')} (being closed once "
            "this email is sent).\n"
            "Nothing is red now because of them; a workflow that fails this often is "
            "worth a look."),
        "email_history_cut": (
            f"red-main could not read enough history for {t.name}; alerts may be missing.\n"
            f"What could not be read in full: {facts.get('what', '')}. Late reports are skipped "
            "while this lasts; the current state is still judged when the open issue can be "
            "known. The run log has the details:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            "A reminder follows once a day while it lasts; the issue closes, with one email, "
            "when a later run reads enough history again."),
        "email_history_still": (
            f"red-main still cannot read enough history for {t.name}; alerts may be missing.\n"
            f"What could not be read in full: {facts.get('what', '')}. Latest run:\n"
            f"  {facts.get('run_url', '')}\n\nTracked in: {facts.get('issue_url', '')}"),
        "email_history_ok": (
            f"red-main reads enough history again for {t.name}: history readable again.\n"
            f"  {facts.get('run_url', '')}\n\nClosed: {facts.get('issue_url', '')}"),
        "email_stopped": (
            "A scheduled workflow has stopped running on schedule. Check its state in "
            "GitHub's Actions tab (re-enable it if disabled) and its newest run:\n"
            f"{facts.get('listing', '')}\n\nTracked in: {facts.get('issue_url', '')}\n"
            f"Run: {facts.get('run_url', '')}\n{t.why}\n"
            "A reminder follows once a day while it lasts."),
        "email_still_stopped": (
            f"Still stopped — day {facts.get('days', 0)}:\n{facts.get('listing', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n{t.why}"),
        "email_stopped_changed": (
            "The set of stopped scheduled workflows changed. Stopped now:\n"
            f"{facts.get('listing', '')}\n\nBefore: {facts.get('before', '')}\n"
            f"Tracked in: {facts.get('issue_url', '')}"),
        "email_running_again": (
            "Every scheduled workflow runs on schedule again. Nothing to do.\n"
            f"  {facts.get('run_url', '')}\n\nClosed: {facts.get('issue_url', '')}"),
        "drill": (
            "This is a DRILL. Nothing is wrong and there is nothing to do.\n\n"
            "It proves that a red-main alert reaches both mailboxes and that the\n"
            f"issue channel works: {facts.get('issue_url', '')}"),
    }
    msg = EmailMessage()
    msg["Subject"] = subjects[kind]
    msg["From"] = sender
    msg["To"] = to
    msg["Cc"] = sender
    msg.set_content(bodies[kind])
    return msg


# --- scheduled workflows: pure parts ----------------------------------------

CRON_LINE = re.compile(r"""^\s*- cron: (["'])(.*?)\1\s*(#.*)?$""")
SCHEDULE_KEY = re.compile(r"^(\s*)schedule:(.*)$")
SCHEDULE_ANY = re.compile(r"""\bschedule["']?\s*:""")
CRON_KEY = re.compile(r"\bcron\s*:")
DAILY_CRON = re.compile(r"^([0-9]|[1-5][0-9]) ([0-9]|1[0-9]|2[0-3]) \* \* \*$")
STOPPED_LINE = re.compile(r"^Stopped: (.*)$", re.M)


class Watched(NamedTuple):
    """One scheduled workflow. `repo` None = this repository. `slots` are the
    cron times as minutes after midnight UTC, sorted; `error` says why the
    schedule could not be read (then `slots` is empty and it is not judged)."""
    key: str
    file: str
    repo: str | None
    slots: tuple[int, ...]
    error: str | None


def read_schedule(text: str) -> tuple[bool, tuple[int, ...], str | None]:
    """(has a schedule, its slots, why it cannot be read). ONE layout is
    understood: a `schedule:` key alone on its line, then a block in which
    every line that is not blank and not a comment reads `- cron: "M H * * *"`
    (quoted). Anything else in the block (a flow-style entry, another key,
    another cron shape), a `schedule:` with something after it, a schedule
    key anywhere else on a line (flow style: `on: {schedule: [...]}`), or a
    `cron:` outside such a block makes the schedule unreadable: never a guess.
    A workflow input literally named `schedule` is read as unreadable too: a
    loud false alarm, never a silent miss."""
    lines = text.splitlines()
    code = [(i, l) for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith("#")]
    keys = [(i, m) for i, l in code if (m := SCHEDULE_KEY.match(l))]
    loose = [i for i, l in code if SCHEDULE_ANY.search(l) and not SCHEDULE_KEY.match(l)]
    if loose:            # `on: {schedule: [...]}` and the like: scheduled, but not read
        return True, (), f"line {loose[0] + 1} has a schedule key that is not a block (flow style)"
    if not keys:
        if any(CRON_KEY.search(l) for _, l in code):
            return True, (), "a cron: line outside a schedule: block"
        return False, (), None
    slots, inside = [], set()
    for i, m in keys:
        if m.group(2).split("#", 1)[0].strip():
            return True, (), f"schedule: line {i + 1} is not a block (flow style)"
        indent = len(m.group(1))
        for j, l in code:
            if j <= i:
                continue
            if len(l) - len(l.lstrip()) <= indent:
                break
            inside.add(j)
            c = CRON_LINE.match(l)
            cron = DAILY_CRON.match(c.group(2)) if c else None
            if not cron:
                return True, (), f'line {j + 1} is not - cron: "M H * * *"'
            slots.append(int(cron.group(2)) * 60 + int(cron.group(1)))
    if any(CRON_KEY.search(l) for j, l in code if j not in inside):
        return True, (), "a cron: line outside a schedule: block"
    if not slots:
        return True, (), "a schedule: block with no cron line"
    return True, tuple(sorted(set(slots))), None


def scheduled_workflows(root: Path, mirror_repo: str | None) -> list[Watched]:
    """Every workflow file under root/.github/workflows with a schedule, then
    the public mirror's verify run (source: root/mirror/verify.yml), read on
    `mirror_repo`. A missing mirror source is reported, not skipped."""
    out = []
    wf = root / ".github" / "workflows"
    for path in sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml"))):
        scheduled, slots, error = read_schedule(path.read_text(encoding="utf-8"))
        if scheduled:
            out.append(Watched(path.name, path.name, None, slots, error))
    if mirror_repo:
        src = root / "mirror" / "verify.yml"
        key = f"verify.yml (on {mirror_repo})"
        if src.is_file():
            scheduled, slots, error = read_schedule(src.read_text(encoding="utf-8"))
            out.append(Watched(key, "verify.yml", mirror_repo, slots,
                               error or (None if scheduled else "mirror/verify.yml has no schedule")))
        else:
            out.append(Watched(key, "verify.yml", mirror_repo, (), "its source mirror/verify.yml is missing"))
    return out


def threshold(slots: tuple[int, ...]) -> timedelta:
    """The longest interval between consecutive cron slots (around midnight
    too; one slot a day = 24 h), plus SCHEDULE_SLACK."""
    gaps = [(b - a) for a, b in zip(slots, slots[1:])] + [slots[0] + 1440 - slots[-1]]
    return timedelta(minutes=max(gaps)) + SCHEDULE_SLACK


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f} h"


def judge(w: Watched, info: dict | None, newest: dict | None, now: datetime) -> str | None:
    """Why `w` is STOPPED, or None while it runs. `info` is GitHub's workflow
    (None: GitHub has none for this file); `newest` its newest scheduled run."""
    if info is None:
        return "GitHub has no workflow for this file"
    if info.get("state") != "active":
        return f"GitHub shows it as {info.get('state')}"
    limit = threshold(w.slots)
    registered = _at(info["created_at"])
    if newest is None:
        if now - registered > limit:
            return (f"no scheduled run since GitHub registered it at {info['created_at']} "
                    f"({_hours(now - registered)}; threshold {_hours(limit)})")
        return None
    start = max(_at(newest["created_at"]), registered)
    if now - start > limit:
        return (f"no scheduled run for {_hours(now - start)} (threshold {_hours(limit)}); "
                f"newest: {newest['created_at']}")
    return None


CHANGE_HEAD = "The set of stopped workflows changed."
RED_MAIN_LOGIN = "github-actions[bot]"     # the identity red-main's GITHUB_TOKEN posts as


def _record(comment: dict) -> bool:
    """A change record: posted by red-main's own token identity, never edited,
    and starting with the change heading. A person's look-alike comment, or an
    edited record, is not one."""
    return ((comment.get("user") or {}).get("login") == RED_MAIN_LOGIN
            and comment.get("created_at") is not None
            and comment.get("updated_at") == comment.get("created_at")
            and (comment.get("body") or "").startswith(CHANGE_HEAD))


def _stopped(text: str) -> set[str] | None:
    m = STOPPED_LINE.search(text or "")
    return None if m is None else {k.strip() for k in m.group(1).split(";") if k.strip()}


def told_set(issue: dict, comments: list[dict]) -> set[str] | None:
    """The set of stopped workflows last told: the newest change RECORD's
    "Stopped:" line if there is one (a change is told by its comment first;
    see _record), else the body's; None if neither has the line."""
    for c in reversed(comments):                      # oldest first, as GitHub lists them
        if _record(c) and (told := _stopped(c["body"])) is not None:
            return told
    return _stopped(issue.get("body") or "")


def schedules_body(problems: dict[str, str]) -> str:
    return (f"{SCHEDULES.subject} has stopped running on schedule.\n\n" + _listing(problems)
            + "\n\nThis issue is commented once a day while it lasts, and when the set of "
            "stopped workflows changes; it closes itself when every scheduled workflow runs "
            "again (`scripts/red_main_alert.py --target schedules`).\n\n"
            + f"Stopped: {'; '.join(sorted(problems))}")


def _listing(problems: dict[str, str]) -> str:
    return "\n".join(f"- `{k}`: {problems[k]}" for k in sorted(problems))


# --- effects ----------------------------------------------------------------

class HistoryCut(Exception):
    """The page cap was reached before enough history was read: what was read
    is never taken as complete. `runs` holds what was read."""
    def __init__(self, what: str, runs: list | None = None):
        super().__init__(what)
        self.what, self.runs = what, runs or []


class GitHub:
    """The few REST calls this needs, on the workflow's own token."""
    def __init__(self, repo: str, token: str, *, api: str = "https://api.github.com",
                 _open=urllib.request.urlopen):
        self.repo, self.token, self.api, self._open = repo, token, api, _open

    def call(self, method: str, path: str, body: dict | None = None, *,
             repo: str | None = None, auth: bool = True):
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"{self.api}/repos/{repo or self.repo}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers)
        with self._open(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")

    def workflow_runs(self, workflow: str, repo: str | None = None, *,
                      enough=None, max_pages: int = MAX_PAGES) -> list[dict]:
        """Completed runs of `workflow` on main, newest first, page by page
        until `enough(runs read so far)` is true, a page is short (the end of
        the history), or `max_pages` pages were read; one page without
        `enough`. `repo` names ANOTHER, public repository (the mirror): this
        workflow's token is scoped to its own repo, so if GitHub refuses it
        there the read is repeated without a token — public run lists need none."""
        runs, auth = [], True
        for page in range(1, max_pages + 1):
            path = (f"/actions/workflows/{workflow}/runs"
                    f"?branch=main&status=completed&per_page={PER_PAGE}&page={page}")
            try:
                batch = self.call("GET", path, repo=repo, auth=auth)["workflow_runs"]
            except urllib.error.HTTPError as exc:
                if repo is None or not auth or exc.code not in (401, 403, 404):
                    raise
                auth = False
                batch = self.call("GET", path, repo=repo, auth=False)["workflow_runs"]
            runs += batch
            if len(batch) < PER_PAGE or enough is None or enough(runs):
                return runs
        raise HistoryCut(f"the run history of {workflow}", runs)

    def _issues(self, query: str, max_pages: int = 20) -> list[dict]:
        out = []
        for page in range(1, max_pages + 1):
            batch = self.call("GET", f"/issues?{query}&per_page={PER_PAGE}&page={page}")
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < PER_PAGE:
                return out
        raise HistoryCut("the issues")

    def _read(self, path: str, repo: str | None) -> dict | None:
        """GET `path`; None on 404. For ANOTHER repository (the public mirror)
        a refusal of this workflow's token is retried without one."""
        try:
            return self.call("GET", path, repo=repo)
        except urllib.error.HTTPError as exc:
            if repo is not None and exc.code in (401, 403, 404):
                try:
                    return self.call("GET", path, repo=repo, auth=False)
                except urllib.error.HTTPError as again:
                    if again.code == 404:
                        return None
                    raise
            if exc.code == 404:
                return None
            raise

    def workflow_info(self, file: str, repo: str | None = None) -> dict | None:
        """GitHub's record of the workflow (state, created_at); None if it has none."""
        return self._read(f"/actions/workflows/{file}", repo)

    def newest_scheduled_run(self, file: str, repo: str | None = None) -> dict | None:
        """The newest run the schedule started, in any status; None if none."""
        got = self._read(f"/actions/workflows/{file}/runs?event=schedule&per_page=1", repo)
        if got is None:
            raise urllib.error.HTTPError(file, 404, "workflow runs not found", None, None)
        runs = got["workflow_runs"]
        return runs[0] if runs else None

    def issue_comments(self, number: int, max_pages: int = 20) -> list[dict]:
        """Every comment of the issue, oldest first, page by page to the end;
        a listing cut at `max_pages` raises HistoryCut."""
        out = []
        for page in range(1, max_pages + 1):
            batch = self.call("GET", f"/issues/{number}/comments?per_page={PER_PAGE}&page={page}")
            out += batch
            if len(batch) < PER_PAGE:
                return out
        raise HistoryCut(f"the comments of issue {number}")

    def edit_body(self, number: int, body: str) -> None:
        self.call("PATCH", f"/issues/{number}", {"body": body})

    def ci_runs(self) -> list[dict]:
        return self.workflow_runs(CI.workflow)

    def issues_since(self, title: str, since: datetime) -> list[dict]:
        """The issues with this title, open or closed, updated since `since`."""
        stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        return [i for i in self._issues(f"state=all&since={stamp}") if i["title"] == title]

    def open_issue(self, title: str) -> dict | None:
        return next((i for i in self._issues("state=open") if i["title"] == title), None)

    def fault_issues(self) -> list[dict]:
        """The open issues labelled FAULT_LABEL, every page to the end: a short
        list of its own, independent of the issue listing that may be cut. A
        listing that cannot be read to its end raises (HistoryCut, or GitHub's
        own error)."""
        return self._issues(f"state=open&labels={FAULT_LABEL}")

    def ensure_fault_label(self) -> None:
        """Confirm FAULT_LABEL exists: read it; on 404 create it (422 there means
        another run just did). Anything else raises: the label is not confirmed."""
        try:
            self.call("GET", f"/labels/{FAULT_LABEL}")
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        try:
            self.call("POST", "/labels", {"name": FAULT_LABEL, "color": "b60205",
                                          "description": "red-main could not read enough history"})
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise

    def add_labels(self, number: int, labels: list[str]) -> list:
        """Add labels to an issue; GitHub answers with the issue's labels."""
        return self.call("POST", f"/issues/{number}/labels", {"labels": labels}) or []

    def create_issue(self, title: str, body: str, owner: str, labels: list[str] | None = None) -> dict:
        return self.call("POST", "/issues", {"title": title, "body": body, "assignees": [owner],
                                             **({"labels": labels} if labels else {})})

    def comment(self, number: int, body: str) -> None:
        self.call("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int) -> None:
        self.call("PATCH", f"/issues/{number}", {"state": "closed"})


def send(msg: EmailMessage, *, user: str, password: str, _smtp=smtplib.SMTP) -> None:
    # 587 + STARTTLS, the same path the server's digest uses.
    with _smtp("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def notify(kind: str, facts: dict, env: dict, *, required: bool = False,
           target: Target = CI, _smtp=smtplib.SMTP) -> bool:
    """Send one alert email. True = sent, or skipped because email is not set up;
    False = failed. With required=True (a drill) "not set up" is a failure too:
    a drill exists to prove delivery, so it may not pass without sending."""
    user, password, to = (env.get(k, "") for k in
                          ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"))
    if not (user and password and to):
        if required:
            print("[red-main] EMAIL-NOT-CONFIGURED (ALERT_* secrets missing) — "
                  "a required email (a drill, or the scheduled-workflows target) cannot be sent")
            return False
        print("[red-main] email not configured (ALERT_* secrets missing) — skipped")
        return True
    try:
        send(build_email(kind, facts, sender=user, to=to, target=target),
             user=user, password=password, _smtp=_smtp)
        print(f"[red-main] email sent: {kind}")
        return True
    except Exception as exc:  # noqa: BLE001 — report it; the issue is already filed
        print(f"[red-main] EMAIL-FAILED ({kind}): {type(exc).__name__}: {str(exc)[:160]}")
        return False


def report_late(gh: GitHub, env: dict, streak: list[dict], ended_by: dict, *,
                owner: str, now: datetime, target: Target, start_known: bool = True,
                _smtp=smtplib.SMTP) -> bool:
    """One red streak no one was told of, told now, in order: the issue is
    opened (naming every run of the streak) and the red email sent; then the
    issue is closed at the success that ended it and the green email sent.
    `start_known` is False when no success precedes the streak in GitHub's
    listing: its first red run may be older than the oldest one listed."""
    latest, first = streak[0], streak[-1]
    began = first["created_at"] + ("" if start_known else " or earlier")
    facts = {"sha": latest["head_sha"], "run_url": latest["html_url"],
             "first_red_at": began, "first_red_sha": first["head_sha"],
             "failed_runs": len(streak), "days": days_red(first["created_at"], now), "late": True}
    runs = "\n".join(f"- {r['html_url']} ({r['conclusion']}, {r['created_at']})" for r in streak)
    issue = gh.create_issue(
        target.title,
        f"{target.subject} failed, and a newer run was already green when red-main "
        f"read it (register T-38), so this is reported late and closed at once.\n\n"
        f"Failed runs (as GitHub lists them):\n{runs}\n\n- First red run: {began} "
        f"(commit `{first['head_sha'][:7]}`)\n- Green again: {ended_by['html_url']}", owner)
    facts["issue_url"] = issue["html_url"]
    print(f"[red-main] {target.label}: a red streak of {len(streak)} run(s) since "
          f"{began} was never reported; reporting it late")
    if not notify("email_red", facts, env, target=target, _smtp=_smtp):
        print(f"[red-main] {target.label}: the late red email failed; the issue stays open "
              "and nothing more is reported late in this run")
        return False
    gh.comment(issue["number"], f"Green again at `{ended_by['head_sha'][:7]}`: {ended_by['html_url']}")
    gh.close(issue["number"])
    green = {"sha": ended_by["head_sha"], "run_url": ended_by["html_url"], "issue_url": issue["html_url"]}
    return notify("email_green", green, env, target=target, _smtp=_smtp)


def report_late_summary(gh: GitHub, env: dict, missed: list[tuple[list[dict], dict]], *,
                        owner: str, target: Target, start_known: bool = True,
                        _smtp=smtplib.SMTP) -> bool:
    """The streaks beyond MAX_LATE, told once: one issue naming every run of
    them, closed once its email is sent, and one email. `start_known` is
    False when the oldest of them is the oldest decisive run read: it may
    have begun earlier."""
    lines = [f"- {r['html_url']} ({r['conclusion']}, {r['created_at']})"
             for streak, _ in missed for r in reversed(streak)]
    oldest = missed[0][0][-1]
    issue = gh.create_issue(
        target.title,
        f"{len(missed)} more red streaks of {target.subject} were never reported when "
        f"they happened (register T-38); each ended green. Reported here in one issue, "
        f"closed once its email is sent.\n\nFailed runs, oldest first:\n" + "\n".join(lines), owner)
    print(f"[red-main] {target.label}: {len(missed)} more unreported red streaks; one summary")
    if not notify("email_late_summary",
                  {"streaks": len(missed),
                   "first_red_at": oldest["created_at"] + ("" if start_known else " or earlier"),
                   "run_url": oldest["html_url"], "issue_url": issue["html_url"]},
                  env, target=target, _smtp=_smtp):
        print(f"[red-main] {target.label}: the summary email failed; its issue stays open")
        return False
    gh.close(issue["number"])
    return True


def history_title(target: Target) -> str:
    return f"red-main could not read enough history for {target.name}"


def _run_url(env: dict) -> str:
    return (f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY', '')}"
            f"/actions/runs/{env.get('GITHUB_RUN_ID', '')}")


def _open_fault(gh: GitHub, title: str) -> dict | None:
    return next((i for i in gh.fault_issues() if i.get("title") == title), None)


def _labelled(issue: dict) -> bool:
    return any((l.get("name") if isinstance(l, dict) else l) == FAULT_LABEL
               for l in issue.get("labels") or [])


def history_fault(gh: GitHub, env: dict, what: str, *, target: Target, event: str,
                  owner: str, _smtp=smtplib.SMTP) -> int:
    """A read that hit its page cap, told through its own issue (labelled
    FAULT_LABEL): the first time, the issue is opened and one email sent;
    while it lasts, only the daily schedule comments and reminds. If the open
    fault issues cannot be listed to their end, nothing is done (no issue, no
    email). The run fails (exit 1) every time."""
    print(f"[red-main] {target.label}: could not read enough history ({what}); "
          "late reports skipped; alerts may be missing")
    title = history_title(target)
    facts = {"what": what, "run_url": _run_url(env)}
    try:
        issue = _open_fault(gh, title)
    except Exception as exc:  # noqa: BLE001 — unknown: never a second issue or email
        print(f"[red-main] the open fault issues cannot be listed ({type(exc).__name__}); "
              "nothing more is done about the fault in this run")
        return 1
    if issue is None:
        # FAIL CLOSED: a fault issue a later run cannot find (no label) would
        # be opened, and emailed, again on every run. So the label is
        # confirmed first, and an issue that cannot carry it is closed at once.
        try:
            gh.ensure_fault_label()
        except Exception as exc:  # noqa: BLE001
            print(f"[red-main] the label {FAULT_LABEL} cannot be confirmed ({type(exc).__name__}); "
                  "no fault issue is opened and no email sent in this run")
            return 1
        issue = gh.create_issue(
            title, f"{title}; alerts may be missing.\n\n- What could not be read in full: {what}\n"
            f"- Run: {facts['run_url']}\n\nThis issue is commented once a day while it lasts and "
            "closes itself when a later run reads enough history (`scripts/red_main_alert.py`).",
            owner, labels=[FAULT_LABEL])
        number = issue.get("number")
        if number is None:          # cannot be labelled, closed or found: no email
            print("[red-main] the new fault issue came back without a number: it cannot be "
                  "labelled or closed, and no email is sent")
            return 1
        if not _labelled(issue):
            try:
                labelled = _labelled({"labels": gh.add_labels(number, [FAULT_LABEL])})
            except Exception:  # noqa: BLE001
                labelled = False
            if not labelled:
                print(f"[red-main] the new fault issue could not carry the label {FAULT_LABEL}; "
                      "closing it; no email is sent")
                try:                # the close first, on its own
                    gh.close(number)
                except Exception as exc:  # noqa: BLE001
                    print(f"[red-main] … the close could not be confirmed ({type(exc).__name__}); "
                          "the issue may still be open")
                    return 1
                print("[red-main] … it is closed")
                try:                # then, only once GitHub accepted the close, say so
                    gh.comment(number, f"Closed: the label {FAULT_LABEL} could not be set, "
                                       "so a later run could not find this issue.")
                except Exception as exc:  # noqa: BLE001
                    print(f"[red-main] … closed, but the comment saying why failed ({type(exc).__name__})")
                return 1
        facts["issue_url"] = issue["html_url"]
        notify("email_history_cut", facts, env, target=target, _smtp=_smtp)
    elif event == "schedule":
        facts["issue_url"] = issue["html_url"]
        gh.comment(issue["number"], f"Still cannot read enough history ({what}). Latest: {facts['run_url']}")
        notify("email_history_still", facts, env, target=target, _smtp=_smtp)
    return 1


def history_readable(gh: GitHub, env: dict, *, target: Target, _smtp=smtplib.SMTP) -> bool:
    """Enough history was read: an open fault issue is closed, with one email.
    False when the fault issues cannot be listed (nothing is done) or the
    email fails."""
    try:
        issue = _open_fault(gh, history_title(target))
    except Exception as exc:  # noqa: BLE001
        print(f"[red-main] the open fault issues cannot be listed ({type(exc).__name__})")
        return False
    if issue is None:
        return True
    gh.comment(issue["number"], f"History readable again: {_run_url(env)}")
    gh.close(issue["number"])
    print(f"[red-main] {target.label}: history readable again; its issue is closed")
    return notify("email_history_ok", {"run_url": _run_url(env), "issue_url": issue["html_url"]},
                  env, target=target, _smtp=_smtp)


def run(gh: GitHub, env: dict, *, event: str, drill: bool = False,
        now: datetime | None = None, _smtp=smtplib.SMTP,
        target: Target = CI, watch_repo: str | None = None, trigger: dict | None = None) -> int:
    """Exit code: 0 = done; 1 = an email that should have gone did not.
    `watch_repo` is the repository whose runs are read when it is not the one
    the issue is filed in (the public mirror). `trigger` is the run that
    started this job (a workflow_run event of this target's workflow)."""
    now = now or datetime.now(timezone.utc)
    owner = env.get("GITHUB_REPOSITORY_OWNER", "")
    if drill:
        issue = gh.create_issue(
            DRILL_TITLE, "A drill of the red-main alert. Nothing is wrong. "
            "This issue is opened and closed by the drill itself.", owner)
        ok = notify("drill", {"issue_url": issue["html_url"]}, env,
                    required=True, _smtp=_smtp)
        gh.comment(issue["number"], "Drill email sent." if ok else
                   "Drill email NOT sent (not configured, or SMTP failed) — see the run log.")
        gh.close(issue["number"])
        print(f"[red-main] drill done: {issue['html_url']}")
        return 0 if ok else 1

    since = now - timedelta(days=LOOKBACK_DAYS)
    cut = None
    try:
        runs = gh.workflow_runs(target.workflow, watch_repo,
                                enough=lambda rs: enough_history(rs, since))
    except HistoryCut as exc:
        runs, cut = exc.runs, exc.what
    runs = with_trigger(runs, trigger)
    state = main_state(runs)
    known = True                       # whether the target's open issue could be known
    try:
        issue = gh.open_issue(target.title)
    except HistoryCut as exc:
        issue, known, cut = None, False, cut or exc.what
    issues: list[dict] = []
    if cut is None and runs:
        try:
            issues = gh.issues_since(target.title, min(since, _at(runs[-1]["created_at"])))
        except HistoryCut as exc:
            cut = exc.what
    # ORDER: the target's own handling first, whenever its open issue is
    # knowable; then the fault path. Each in its own guard: a failure of one
    # never suppresses the other, and the run fails if either did.
    ok = True
    if state is None:
        print(f"[red-main] no decisive {target.workflow} run for {target.label} "
              "yet — nothing to do")
    elif known:
        try:
            ok = _ordinary(gh, env, runs, state, issue, issues, cut, since=since, now=now,
                           event=event, owner=owner, target=target, _smtp=_smtp)
        except Exception as exc:  # noqa: BLE001 — reported; the fault path still runs
            print(f"[red-main] {target.label}: the alert itself failed ({type(exc).__name__}: "
                  f"{str(exc)[:160]})")
            ok = False
    try:
        fault_ok = (history_fault(gh, env, cut, target=target, event=event, owner=owner,
                                  _smtp=_smtp) == 0) if cut else \
            history_readable(gh, env, target=target, _smtp=_smtp)
    except Exception as exc:  # noqa: BLE001
        print(f"[red-main] the history fault could not be handled ({type(exc).__name__})")
        fault_ok = False
    return 0 if ok and fault_ok and known else 1


def _ordinary(gh: GitHub, env: dict, runs: list[dict], state: dict, issue: dict | None,
              issues: list[dict], cut: str | None, *, since: datetime, now: datetime,
              event: str, owner: str, target: Target, _smtp=smtplib.SMTP) -> bool:
    """The target's own alert: late reports (not from a cut history), then the
    current state. False when an email that should have gone did not."""
    ok = True
    oldest = next((r for r in reversed(runs) if r.get("conclusion") in DECISIVE), None)
    if issue is not None and all(i.get("number") != issue.get("number") for i in issues):
        issues = issues + [issue]
    missed = [] if cut else missed_streaks(runs, issues, since)
    for streak, ended_by in missed[:MAX_LATE]:
        if not report_late(gh, env, streak, ended_by, owner=owner, now=now, target=target,
                           start_known=streak[-1] is not oldest, _smtp=_smtp):
            ok, missed = False, []           # a failed late email stops the late reports
            break
    if missed[MAX_LATE:]:
        ok = report_late_summary(gh, env, missed[MAX_LATE:], owner=owner, target=target,
                                 start_known=missed[MAX_LATE][0][-1] is not oldest,
                                 _smtp=_smtp) and ok
    actions = decide(red=state["red"], issue_open=issue is not None, event=event)
    latest = state["latest"]
    facts = {"sha": latest["head_sha"], "run_url": latest["html_url"]}
    if state["red"]:
        first = state["first_red"]
        began = first["created_at"] + ("" if first is not oldest else " or earlier")
        facts.update(first_red_at=began, first_red_sha=first["head_sha"],
                     failed_runs=state["failed_runs"], days=days_red(first["created_at"], now))
    print(f"[red-main] {target.label} is {'RED' if state['red'] else 'green'} at "
          f"{facts['sha'][:7]}; issue {'open' if issue else 'none'}; "
          f"event {event}; actions {actions or 'none'}")
    for action in actions:
        if action == "open":
            issue = gh.create_issue(
                target.title,
                f"{target.subject} is failing.\n\n- Latest failing run: {facts['run_url']}\n"
                f"- First red run: {facts['first_red_at']} (commit `{first['head_sha'][:7]}`)\n\n"
                "This issue is commented once a day while it stays red and closes "
                "itself when it is green again (`scripts/red_main_alert.py`).", owner)
        elif action == "comment":
            gh.comment(issue["number"],
                       f"Still red — day {facts['days']}, {facts['failed_runs']} failed "
                       f"runs. Latest: {facts['run_url']}")
        elif action == "close":
            gh.comment(issue["number"], f"Green again at `{facts['sha'][:7]}`: "
                                        f"{facts['run_url']}")
            gh.close(issue["number"])
        else:
            facts["issue_url"] = issue["html_url"]
            ok = notify(action, facts, env, target=target, _smtp=_smtp) and ok
    return ok


SCHEDULE_EMAIL = {"email_red": "email_stopped", "email_still_red": "email_still_stopped",
                  "email_green": "email_running_again"}


def run_schedules(gh: GitHub, env: dict, *, event: str, root: Path, mirror_repo: str | None,
                  now: datetime | None = None, _smtp=smtplib.SMTP) -> int:
    """The `schedules` target. Exit 0 = done; 1 = an email that should have
    gone did not, or something could not be read (the fault path)."""
    now = now or datetime.now(timezone.utc)
    owner = env.get("GITHUB_REPOSITORY_OWNER", "")
    problems: dict[str, str] = {}
    unread: list[str] = []
    for w in scheduled_workflows(root, mirror_repo):
        if w.error:          # an unreadable schedule is never skipped: the fault path
            print(f"[red-main] schedules: {w.key}: its schedule cannot be read ({w.error})")
            unread.append(f"{w.key} (its schedule: {w.error})")
            continue
        try:
            info = gh.workflow_info(w.file, w.repo)
            newest = gh.newest_scheduled_run(w.file, w.repo) if info is not None else None
        except Exception as exc:  # noqa: BLE001 — unknown is never "running"
            unread.append(f"{w.key} ({type(exc).__name__})")
            continue
        why = judge(w, info, newest, now)
        print(f"[red-main] schedules: {w.key}: "
              + (f"STOPPED — {why}" if why else
                 f"running (newest scheduled run {newest['created_at'] if newest else 'none yet'}; "
                 f"threshold {_hours(threshold(w.slots))})"))
        if why:
            problems[w.key] = why
    issue, comments, cut = None, [], None
    if unread:
        cut = "the schedule, state or scheduled runs of " + ", ".join(unread)
    else:
        try:
            issue = gh.open_issue(SCHEDULES.title)
            comments = gh.issue_comments(issue["number"]) if issue is not None else []
        except Exception as exc:  # noqa: BLE001
            cut = f"the open issue or its comments ({type(exc).__name__})"
    if cut:   # a partial read is never taken as "all running": the fault path alone
        try:
            history_fault(gh, env, cut, target=SCHEDULES, event=event, owner=owner, _smtp=_smtp)
        except Exception as exc:  # noqa: BLE001
            print(f"[red-main] the fault could not be handled ({type(exc).__name__})")
        return 1
    try:
        ok = _schedules_ordinary(gh, env, problems, issue, comments, event=event, now=now,
                                 owner=owner, _smtp=_smtp)
    except Exception as exc:  # noqa: BLE001 — reported; the fault path still runs
        print(f"[red-main] schedules: the alert itself failed ({type(exc).__name__}: {str(exc)[:160]})")
        ok = False
    try:
        fault_ok = history_readable(gh, env, target=SCHEDULES, _smtp=_smtp)
    except Exception as exc:  # noqa: BLE001
        print(f"[red-main] the fault issue could not be handled ({type(exc).__name__})")
        fault_ok = False
    return 0 if ok and fault_ok else 1


def _schedules_ordinary(gh: GitHub, env: dict, problems: dict[str, str], issue: dict | None,
                        comments: list[dict], *, event: str, now: datetime, owner: str,
                        _smtp=smtplib.SMTP) -> bool:
    """False when an email that should have gone did not: the run then fails
    and red-main's heartbeat posts /fail. A lost email is reported by the
    heartbeat, not retried (no pending state is kept across runs)."""
    ok, red = True, bool(problems)
    names = "; ".join(sorted(problems))
    facts = {"names": names, "listing": _listing(problems), "run_url": _run_url(env)}
    actions = decide(red=red, issue_open=issue is not None, event=event)
    before = told_set(issue, comments) if issue is not None else None
    if red and issue is not None and before != set(problems):
        # ORDER: the comment (naming the new set) is the record; then the email;
        # then the body. A failed comment raises: no email, the run fails. A body
        # that will not update cannot repeat the email: the comment already told it.
        facts.update(issue_url=issue["html_url"],
                     before="; ".join(sorted(before)) if before is not None else "unknown")
        gh.comment(issue["number"], f"{CHANGE_HEAD} Stopped now:\n{facts['listing']}\n\n"
                                    f"Run: {facts['run_url']}\n\nStopped: {names}")
        ok = notify("email_stopped_changed", facts, env, required=True, target=SCHEDULES,
                    _smtp=_smtp)
        gh.edit_body(issue["number"], schedules_body(problems))
        actions = [a for a in actions if a not in ("comment", "email_still_red")]
    print(f"[red-main] schedules: {len(problems)} stopped; issue {'open' if issue else 'none'}; "
          f"event {event}; actions {actions or 'none'}")
    for action in actions:
        if action == "open":
            issue = gh.create_issue(SCHEDULES.title, schedules_body(problems), owner)
        elif action == "comment":
            facts["days"] = days_red(issue["created_at"], now)
            gh.comment(issue["number"], f"Still stopped — day {facts['days']}:\n{facts['listing']}\n\n"
                                        f"Run: {facts['run_url']}")
        elif action == "close":
            gh.comment(issue["number"], f"Every scheduled workflow runs again: {facts['run_url']}")
            gh.close(issue["number"])
        else:
            facts["issue_url"] = issue["html_url"]
            # required: missing email settings are a lost email too (exit 1, the
            # heartbeat's /fail), on this target only; the other five skip and say so
            ok = notify(SCHEDULE_EMAIL[action], facts, env, required=True, target=SCHEDULES,
                        _smtp=_smtp) and ok
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drill", action="store_true",
                    help="send a test email and open+close a drill issue")
    ap.add_argument("--target", choices=sorted(TARGETS), default="ci",
                    help="which workflow to watch (default: this repo's CI)")
    ap.add_argument("--repo", default=None,
                    help="OWNER/NAME of the public repository whose runs are "
                         "read; required with --target mirror-verify and --target schedules")
    args = ap.parse_args(argv)
    if args.target in ("mirror-verify", "schedules") and not args.repo:
        ap.error(f"--target {args.target} needs --repo OWNER/NAME (the mirror)")
    env = dict(os.environ)
    gh = GitHub(env["GITHUB_REPOSITORY"], env["GITHUB_TOKEN"],
                api=env.get("GITHUB_API_URL", "https://api.github.com"))
    target = TARGETS[args.target]
    if target is SCHEDULES:
        if args.drill:
            ap.error("--drill is not a schedules run")
        return run_schedules(gh, env, event=env.get("GITHUB_EVENT_NAME", ""),
                             root=Path(__file__).resolve().parents[1], mirror_repo=args.repo)
    return run(gh, env, event=env.get("GITHUB_EVENT_NAME", ""), drill=args.drill,
               target=target, watch_repo=args.repo,
               trigger=triggering_run(env, target, args.repo))


def triggering_run(env: dict, target: Target, watch_repo: str | None) -> dict | None:
    """The run that started this job, from the event payload, if it is a run of
    this target's workflow in the repository this target reads; else None."""
    if env.get("GITHUB_EVENT_NAME") != "workflow_run" or not env.get("GITHUB_EVENT_PATH"):
        return None
    try:
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
            wr = json.load(f).get("workflow_run") or {}
    except (OSError, ValueError) as exc:
        print(f"[red-main] the event payload could not be read ({type(exc).__name__}); "
              "the run list alone is used")
        return None
    same_repo = (wr.get("repository") or {}).get("full_name") == (watch_repo or env.get("GITHUB_REPOSITORY"))
    if not same_repo or not str(wr.get("path", "")).endswith(f"/{target.workflow}"):
        return None
    return {k: wr.get(k) for k in ("id", "conclusion", "created_at", "head_sha", "html_url")}


if __name__ == "__main__":
    sys.exit(main())
