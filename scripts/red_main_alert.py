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
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

TITLE = "CI is red on main"
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


def build_email(kind: str, facts: dict, *, sender: str, to: str) -> EmailMessage:
    """The action wanted comes first; the alert account is always copied."""
    sha = facts.get("sha", "")[:7]
    subjects = {
        "email_red": f"talea CI RED on main — {sha}",
        "email_still_red": f"talea CI still RED on main — day {facts.get('days', 0)}",
        "email_green": f"talea CI green again on main — {sha}",
        "drill": "[DRILL] talea red-main alert — delivery test",
    }
    bodies = {
        "email_red": (
            "CI on main is failing. Open the failing run and fix or escalate:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            f"First red run: {facts.get('first_red_at', '')} (commit {sha}).\n"
            "A reminder follows once a day until main is green."),
        "email_still_red": (
            f"CI on main is STILL failing — day {facts.get('days', 0)}, "
            f"{facts.get('failed_runs', 0)} failed runs. Open the latest failing run:\n"
            f"  {facts.get('run_url', '')}\n\n"
            f"Tracked in: {facts.get('issue_url', '')}\n"
            f"Red since: {facts.get('first_red_at', '')}.\n"
            "While main is red, no resolver PR can merge."),
        "email_green": (
            "CI on main is green again. Nothing to do.\n"
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

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with self._open(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")

    def ci_runs(self) -> list[dict]:
        return self.call("GET", "/actions/workflows/ci.yml/runs"
                                "?branch=main&status=completed&per_page=100")["workflow_runs"]

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
           _smtp=smtplib.SMTP) -> bool:
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
        send(build_email(kind, facts, sender=user, to=to),
             user=user, password=password, _smtp=_smtp)
        print(f"[red-main] email sent: {kind}")
        return True
    except Exception as exc:  # noqa: BLE001 — report it; the issue is already filed
        print(f"[red-main] EMAIL-FAILED ({kind}): {type(exc).__name__}: {str(exc)[:160]}")
        return False


def run(gh: GitHub, env: dict, *, event: str, drill: bool = False,
        now: datetime | None = None, _smtp=smtplib.SMTP) -> int:
    """Exit code: 0 = done; 1 = an email that should have gone did not."""
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

    state = main_state(gh.ci_runs())
    if state is None:
        print("[red-main] no decisive CI run on main yet — nothing to do")
        return 0
    issue = gh.open_issue(TITLE)
    actions = decide(red=state["red"], issue_open=issue is not None, event=event)
    latest = state["latest"]
    facts = {"sha": latest["head_sha"], "run_url": latest["html_url"]}
    if state["red"]:
        first = state["first_red"]
        facts.update(first_red_at=first["created_at"], failed_runs=state["failed_runs"],
                     days=days_red(first["created_at"], now))
    print(f"[red-main] main is {'RED' if state['red'] else 'green'} at "
          f"{facts['sha'][:7]}; issue {'open' if issue else 'none'}; "
          f"event {event}; actions {actions or 'none'}")
    ok = True
    for action in actions:
        if action == "open":
            issue = gh.create_issue(
                TITLE,
                f"CI on `main` is failing.\n\n- Latest failing run: {facts['run_url']}\n"
                f"- First red run: {facts['first_red_at']} (commit `{first['head_sha'][:7]}`)\n\n"
                "This issue is commented once a day while main stays red and closes "
                "itself when main is green again (`scripts/red_main_alert.py`).", owner)
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
            ok = notify(action, facts, env, _smtp=_smtp) and ok
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drill", action="store_true",
                    help="send a test email and open+close a drill issue")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    gh = GitHub(env["GITHUB_REPOSITORY"], env["GITHUB_TOKEN"],
                api=env.get("GITHUB_API_URL", "https://api.github.com"))
    return run(gh, env, event=env.get("GITHUB_EVENT_NAME", ""), drill=args.drill)


if __name__ == "__main__":
    sys.exit(main())
