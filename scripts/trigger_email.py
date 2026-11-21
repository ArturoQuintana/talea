"""Email the operator what set a red trigger check's verdict (register T-41, C2)

    ALERT_SMTP_USER=… ALERT_SMTP_PASSWORD=… ALERT_EMAIL_TO=… WORKFLOW=… RUN_URL=… COMMIT=… \
        python3 -I scripts/trigger_email.py ITEMS_FILE LABEL_FILE

Ruled by the operator 2026-11-20 ("D-36: C1 + C2"): each firing is named to
him in an email within a day of the check's run. When a trigger check's run is
red (receipt-push-times: T3/T4; write-gap: T1; stamp-margin: T2), its
workflow's last step runs this script. It emails the items that set the
verdict, which the check wrote to ITEMS_FILE as "<date or -> <line>", newest
first. LABEL_FILE holds the step's label, e.g. "TRIGGER T2 (D-36): …". The
subject carries the label, the count and the newest receipt's date. It holds
no state. Each red run sends its own email, and red-main is unchanged.

The sending is a COPY of the writers' failure email (the notify job in
ercot.yml and jp.yml): STARTTLS that verifies the server's certificate and
name (`ssl.create_default_context()`, in both since register T-42) before the
alert account's credentials are sent, the same account, recipient and copy,
the same 587 path, and the same failure handling: one try around everything from
the message to the server's answer; only exception types, numeric SMTP codes
and fixed words printed, never an address; exit 1 when the settings are
missing or the operator's address is refused. The writers keep their own
inline copy. Their notify job deliberately uses nothing from the checkout
(it must still email when the tick failed before or after the checkout), and
it is a drilled, confirmed channel (2026-11-14), so it is not rewired here.

Only the check's own lines are emailed: dates, UTC times, whole milliseconds,
seconds, short commit ids, registered (strategy, version) pairs and fixed
words. No price, nothing from a commit's body. Standard library only.
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
WHAT = ("A D-36 trigger (T1, T2, T3, T4) reopens D-36 for your decision; it changes no rule by "
        "itself. A line marked \"not a trigger\" is detector health: the check could not judge, "
        "and it never reopens D-36. Lines from before the start date (information) are not "
        "listed. Each run judges the last 7 days again, so a line stays in each day's email "
        "until it leaves that window, and nothing marks a line as new: the lines are ordered by "
        "their receipt's target date, newest first (the same date: by their text, descending; no "
        "date: last). The run's log is the authority.")


def compose(items_text: str | None, label: str | None, env: dict) -> tuple[str, str]:
    """(subject, body). `items_text` None: the check did not write its list."""
    workflow, url, commit = env.get("WORKFLOW") or "a trigger check", env.get("RUN_URL"), env.get("COMMIT")
    label = (label or "").strip().splitlines()[0] if (label or "").strip() else \
        "CANNOT JUDGE (not a trigger): the check did not finish"
    short = label.split(":", 1)[0]
    if items_text is None:
        lines, newest = [], None
        count = "no list"
    else:
        lines = [l for l in items_text.splitlines() if l.strip()]
        days = [l.split(" ", 1)[0] for l in lines if DAY.fullmatch(l.split(" ", 1)[0])]
        newest = max(days) if days else None
        count = f"{len(lines)} item{'s' if len(lines) != 1 else ''}"
    subject = f"talea {workflow} RED, {short}: {count}" + (f", newest receipt {newest}" if newest else "")
    listing = (["What set the verdict, newest first:", *lines] if lines else
               ["The check wrote no list (it did not finish, or a step before it failed); read the run's log."]
               if items_text is None else ["The check listed nothing that sets the verdict; read the run's log."])
    body = "\n".join([f"{workflow}: red. {label}", f"Run: {url}", f"Commit: {commit}", "", *listing, "", WHAT]) + "\n"
    return subject, body


def main(argv: list[str], _smtp=smtplib.SMTP, _exit=os._exit) -> int:
    items_path, label_path = (Path(a) for a in argv[:2])
    items = items_path.read_text(encoding="utf-8") if items_path.is_file() else None
    label = label_path.read_text(encoding="utf-8") if label_path.is_file() else None
    env = os.environ
    subject, body = compose(items, label, env)
    print(subject, flush=True)
    user, password, to = (env.get(k) for k in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"))
    if not (user and password and to):
        print("EMAIL-NOT-CONFIGURED: ALERT_SMTP_USER, ALERT_SMTP_PASSWORD or "
              "ALERT_EMAIL_TO is missing; no email was sent", file=sys.stderr)
        return 1
    # As the writers' notify step: ONE try from the message to the server's
    # answer; its handler prints the exception's type and numeric codes only and
    # leaves with os._exit(1) (no traceback, nothing chained).
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        msg["Cc"] = user
        msg.set_content(body)
        with _smtp("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())   # the server's certificate is verified
            s.login(user, password)
            refused = s.send_message(msg)
        to_addrs = [addr for _, addr in getaddresses([to]) if addr]
        to_codes = [int(refused[a][0]) for a in to_addrs if a in refused]
        all_codes = [int(code) for code, _ in refused.values()]
    except BaseException as err:
        try:
            if isinstance(err, smtplib.SMTPRecipientsRefused):
                codes = [int(code) for code, _ in err.recipients.values()]
            elif isinstance(getattr(err, "smtp_code", None), int):
                codes = [int(err.smtp_code)]
            else:
                codes = []
        except BaseException:
            codes = []
        try:
            print(f"EMAIL-FAILED: {type(err).__name__} (SMTP codes {codes})", file=sys.stderr, flush=True)
        finally:
            _exit(1)
    if to_codes or not to_addrs:
        print(f"EMAIL-REFUSED: the operator's address was refused or unreadable (SMTP codes {to_codes})",
              file=sys.stderr)
        return 1
    if all_codes:
        print(f"the server refused the copy only (SMTP codes {all_codes})")
    print("email sent")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
