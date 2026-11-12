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

Stdlib-only, like verify_ledger.py. No address or credential lives in this file:
ALERT_SMTP_USER / ALERT_SMTP_PASSWORD / ALERT_EMAIL_TO come from the workflow's
secrets. Without them the email is skipped (and said so) — the issue still files.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
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
TARGETS = {"ci": CI, "mirror-verify": MIRROR_VERIFY}

TITLE = CI.title
DRILL_TITLE = "[DRILL] CI is red on main — delivery test"
RED = {"failure", "timed_out", "startup_failure"}
DECISIVE = RED | {"success"}       # cancelled / skipped runs say nothing either way


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
    }
    bodies = {
        "email_red": (
            f"{t.subject} is failing. Open the failing run and fix or escalate:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            f"First red run: {facts.get('first_red_at', '')} (commit {sha}).\n"
            "A reminder follows once a day until it is green."),
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


# --- effects ----------------------------------------------------------------

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

    def workflow_runs(self, workflow: str, repo: str | None = None) -> list[dict]:
        """Completed runs of `workflow` on main, newest first. `repo` names
        ANOTHER, public repository (the mirror): this workflow's token is scoped
        to its own repo, so if GitHub refuses it there the read is repeated
        without a token — public run lists need none."""
        path = (f"/actions/workflows/{workflow}/runs"
                "?branch=main&status=completed&per_page=100")
        try:
            return self.call("GET", path, repo=repo)["workflow_runs"]
        except urllib.error.HTTPError as exc:
            if repo is None or exc.code not in (401, 403, 404):
                raise
            return self.call("GET", path, repo=repo, auth=False)["workflow_runs"]

    def ci_runs(self) -> list[dict]:
        return self.workflow_runs(CI.workflow)

    def open_issue(self, title: str) -> dict | None:
        for issue in self.call("GET", "/issues?state=open&per_page=100"):
            if issue["title"] == title and "pull_request" not in issue:
                return issue
        return None

    def create_issue(self, title: str, body: str, owner: str) -> dict:
        return self.call("POST", "/issues",
                         {"title": title, "body": body, "assignees": [owner]})

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
                  "the drill cannot prove delivery")
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


def run(gh: GitHub, env: dict, *, event: str, drill: bool = False,
        now: datetime | None = None, _smtp=smtplib.SMTP,
        target: Target = CI, watch_repo: str | None = None) -> int:
    """Exit code: 0 = done; 1 = an email that should have gone did not.
    `watch_repo` is the repository whose runs are read when it is not the one
    the issue is filed in (the public mirror)."""
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

    state = main_state(gh.workflow_runs(target.workflow, watch_repo))
    if state is None:
        print(f"[red-main] no decisive {target.workflow} run for {target.label} "
              "yet — nothing to do")
        return 0
    issue = gh.open_issue(target.title)
    actions = decide(red=state["red"], issue_open=issue is not None, event=event)
    latest = state["latest"]
    facts = {"sha": latest["head_sha"], "run_url": latest["html_url"]}
    if state["red"]:
        first = state["first_red"]
        facts.update(first_red_at=first["created_at"], failed_runs=state["failed_runs"],
                     days=days_red(first["created_at"], now))
    print(f"[red-main] {target.label} is {'RED' if state['red'] else 'green'} at "
          f"{facts['sha'][:7]}; issue {'open' if issue else 'none'}; "
          f"event {event}; actions {actions or 'none'}")
    ok = True
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
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drill", action="store_true",
                    help="send a test email and open+close a drill issue")
    ap.add_argument("--target", choices=sorted(TARGETS), default="ci",
                    help="which workflow to watch (default: this repo's CI)")
    ap.add_argument("--repo", default=None,
                    help="OWNER/NAME of the public repository whose runs are "
                         "read; required with --target mirror-verify")
    args = ap.parse_args(argv)
    if args.target == "mirror-verify" and not args.repo:
        ap.error("--target mirror-verify needs --repo OWNER/NAME (the mirror)")
    env = dict(os.environ)
    gh = GitHub(env["GITHUB_REPOSITORY"], env["GITHUB_TOKEN"],
                api=env.get("GITHUB_API_URL", "https://api.github.com"))
    return run(gh, env, event=env.get("GITHUB_EVENT_NAME", ""), drill=args.drill,
               target=TARGETS[args.target], watch_repo=args.repo)


if __name__ == "__main__":
    sys.exit(main())
