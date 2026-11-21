"""The writer workflows (ercot.yml, jp.yml) email the operator when the tick job
fails or is cancelled.

Why: a failed writer run has not confirmed its push, so a receipt it committed
may exist only on its runner, and a missed receipt is never backfilled. A
scheduled run's failure reaches nobody by itself. The team concluded on
2026-11-14 that a small step in each writer is worth more to the goal than a
separate watcher.

What these tests hold:
  - the email is a job of its own, `notify`, that `needs: tick` and runs on
    `failure() || cancelled()`, with no token and no concurrency group: sending
    can never hold a rung's place (review finding, round 1);
  - the concurrency group is the tick job's. Real runs keep the market's group
    exactly as before; a drill queues in a group of its own, so it can never
    replace a pending real rung (review finding, round 1);
  - a drill (a manual run with `drill` ticked) fails in the tick job's FIRST
    step, before the checkout and the tick, so it never fetches or commits;
  - the notify job is the same text in both writers;
  - the script inside it, run here exactly as written in the workflow against a
    fake smtplib, sends one email naming the workflow, the status, the run, the
    attempt and the link; it fails loudly (exit 1) when a secret is missing,
    when connecting, STARTTLS, login, sending or closing fails, and when the
    server refuses the operator's address (round 1: a refused To with an
    accepted Cc used to count as sent). A failure prints the exception's type
    and SMTP codes only, never its text and never a traceback: the text can
    carry an address (rounds 2 and 3).

Limits, stated in the workflows and INFRA.md: a pending tick replaced by a
newer run may be reported as CANCELLED (that job produced no receipt; whether
today's receipt landed is the newer run's to show); after a cancellation the
email is best effort; if the notify job fails, delivery is uncertain.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import private_file

WRITERS = {"ercot.yml": "ercot", "jp.yml": "jp"}
EMAIL_STEP = "Email the operator that the tick job failed or was cancelled"
DRILL_STEP = "Drill (manual run only), fails before the tick"


def _workflow(name: str) -> str:
    return private_file(f".github/workflows/{name}").read_text()


def _top_keys(text: str) -> list[str]:
    return [l.split(":")[0] for l in text.splitlines()
            if l and not l.startswith((" ", "#")) and ":" in l]


def _jobs(text: str) -> dict[str, list[str]]:
    """Each job under `jobs:`, as its lines (comments dropped), dedented to the
    job's own keys."""
    lines = text.splitlines()
    out: dict[str, list[str]] = {}
    current = None
    for line in lines[lines.index("jobs:") + 1:]:
        if line.strip().startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("   "):
            current = line.strip().rstrip(":")
            out[current] = []
        elif current:
            out[current].append(line[4:])
    return out


def _steps(job: list[str]) -> list[list[str]]:
    """A job's steps, each as its lines dedented to the step's dash."""
    start = job.index("steps:")
    steps: list[list[str]] = []
    for line in job[start + 1:]:
        if not line.startswith("  "):
            break
        if line.startswith("  - "):
            steps.append([line[2:]])
        elif steps:
            steps[-1].append(line[2:])
    return steps


def _script(text: str) -> str:
    """The Python the notify job runs: the text between `python3 - <<'PY'` and
    `PY`, verbatim as the shell receives it."""
    lines = text.splitlines()
    first = lines.index("          python3 - <<'PY'")
    last = lines.index("          PY")
    return textwrap.dedent("\n".join(lines[first + 1:last])) + "\n"


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_concurrency_is_the_tick_jobs_and_a_drill_has_its_own_group(name):
    text = _workflow(name)
    slug = WRITERS[name]
    assert "concurrency" not in _top_keys(text), \
        f"{name}: a workflow-level group would hold the notify job inside it"
    jobs = _jobs(text)
    assert sorted(jobs) == ["key-probe", "notify", "tick"]   # key-probe: E6, a manual check that never ticks
    assert "concurrency" not in "".join(jobs["key-probe"])
    tick = jobs["tick"]
    at = tick.index("concurrency:")
    assert tick[at + 1:at + 3] == [
        f"  group: ${{{{ inputs.drill && '{slug}-tick-drill' || '{slug}-tick' }}}}",
        "  cancel-in-progress: false"], f"{name}: the tick job's group changed"
    assert "notify" not in "".join(tick)
    notify_text = text[text.index("\n  notify:\n"):]
    assert "concurrency" not in notify_text, f"{name}: the notify job must be in no group"


@pytest.mark.parametrize("drill, group", [
    (None, "{slug}-tick"),        # a scheduled run: `inputs` is empty
    (False, "{slug}-tick"),       # a manual run without the drill
    (True, "{slug}-tick-drill"),  # a drill: never the real group
])
def test_the_group_expression_never_puts_a_drill_with_real_runs(drill, group):
    """The expression `inputs.drill && A || B`, evaluated as GitHub does for
    these three values (`&&` and `||` return an operand; null and false are
    falsy, a non-empty string is truthy)."""
    for name, slug in WRITERS.items():
        tick = _jobs(_workflow(name))["tick"]
        line = tick[tick.index("concurrency:") + 1]
        a, b = (f"'{slug}-tick-drill'", f"'{slug}-tick'")
        assert line == f"  group: ${{{{ inputs.drill && {a} || {b} }}}}"
        value = (a if drill else None) or b
        assert value.strip("'") == group.format(slug=slug)
        assert value.strip("'") != f"{slug}-tick" or not drill


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_the_notify_job_runs_after_the_tick_on_failure_or_cancel(name):
    job = _jobs(_workflow(name))["notify"]
    for line in ("needs: tick", "if: failure() || cancelled()", "runs-on: ubuntu-latest",
                 "timeout-minutes: 3", "permissions: {}"):
        assert line in job, f"{name}: the notify job lost {line!r}"
    steps = _steps(job)
    assert len(steps) == 1 and steps[0][0] == f"- name: {EMAIL_STEP}"
    step = steps[0]
    for line in ("    ALERT_SMTP_USER: ${{ secrets.ALERT_SMTP_USER }}",
                 "    ALERT_SMTP_PASSWORD: ${{ secrets.ALERT_SMTP_PASSWORD }}",
                 "    ALERT_EMAIL_TO: ${{ secrets.ALERT_EMAIL_TO }}",
                 "    WORKFLOW: ${{ github.workflow }}",
                 "    STATUS: ${{ needs.tick.result }}",
                 "    EVENT: ${{ github.event_name }}",
                 "    DRILL: ${{ inputs.drill == true }}",
                 "    RUN_ID: ${{ github.run_id }}",
                 "    ATTEMPT: ${{ github.run_attempt }}",
                 "    RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/"
                 "${{ github.run_id }}/attempts/${{ github.run_attempt }}",
                 "    COMMIT: ${{ github.sha }}"):
        assert line in step, f"{name}: the email step lost {line.strip()!r}"
    # No expression is pasted into the script's text: values come through env.
    assert "${{" not in _script(_workflow(name))
    # Nothing from the checkout or from uv: no checkout in this job at all.
    assert not any("uses:" in l or "uv " in l or "scripts/" in l for l in job)


def test_both_writers_carry_the_same_notify_job_and_drill_step():
    ercot, jp = (_jobs(_workflow(n)) for n in sorted(WRITERS))
    assert ercot["notify"] == jp["notify"]
    assert _steps(ercot["tick"])[0] == _steps(jp["tick"])[0]


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_drill_fails_first_before_the_checkout_and_the_tick(name):
    text = _workflow(name)
    assert ("  workflow_dispatch:\n    inputs:\n      drill:\n" in text
            and "        type: boolean\n        default: false\n" in text), \
        f"{name}: workflow_dispatch must offer a boolean `drill` input, false by default"
    steps = _steps(_jobs(text)["tick"])
    assert steps[0][0] == f"- name: {DRILL_STEP}", f"{name}: the drill must be the first step"
    assert "  if: inputs.drill == true" in steps[0]
    assert [l.strip() for l in steps[0] if l.strip()][-1] == "exit 1"
    assert steps[1][0] == "- uses: actions/checkout@v4"
    ticks = [i for i, s in enumerate(steps) if "python -m talea tick" in "\n".join(s)]
    assert len(ticks) == 1 and ticks[0] > 0


# ---- the script itself, run exactly as written in the workflow ----

FAKE_SMTPLIB = '''
import json, os
FAIL = set(os.environ.get("FAKE_SMTP_FAIL", "").split(","))
# The shapes of the real smtplib exceptions this script can meet.
class SMTPException(OSError):
    pass
class SMTPRecipientsRefused(SMTPException):
    def __init__(self, recipients):
        self.recipients = recipients
        self.args = (recipients,)
class SMTPResponseException(SMTPException):
    def __init__(self, code, msg):
        self.smtp_code, self.smtp_error = code, msg
        self.args = (code, msg)
class SMTPSenderRefused(SMTPResponseException):
    def __init__(self, code, msg, sender):
        super().__init__(code, msg)
        self.sender = sender
        self.args = (code, msg, sender)
class SMTPDataError(SMTPResponseException):
    pass
class CodeInterrupted(SMTPException):
    # reading its code is interrupted: a SIGINT landing inside the handler
    @property
    def smtp_code(self):
        raise KeyboardInterrupt()
REFUSE = json.loads(os.environ.get("FAKE_SMTP_REFUSE", "{}"))
class SMTP:
    def __init__(self, host, port, timeout=None):
        self.log = {"host": host, "port": port, "timeout": timeout, "calls": []}
        if "connect" in FAIL:
            raise OSError("connection refused")
    def _record(self):
        with open(os.environ["FAKE_SMTP_LOG"], "a") as f:
            f.write(json.dumps(self.log) + "\\n")
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self._record()
        if "quit" in FAIL:
            raise OSError("quit failed")
        if "quit-interrupt" in FAIL:
            raise KeyboardInterrupt()
        return False
    def starttls(self, context=None):
        if "starttls" in FAIL:
            raise OSError("starttls failed")
        import ssl
        self.log["calls"].append("starttls")
        self.log["tls"] = None if context is None else {
            "is_context": isinstance(context, ssl.SSLContext),
            "check_hostname": context.check_hostname,
            "verify_required": context.verify_mode == ssl.CERT_REQUIRED}
    def login(self, user, password):
        if "login" in FAIL:
            raise OSError("authentication failed")
        self.log["calls"].append(["login", user, password])
    def send_message(self, msg):
        if "send" in FAIL:
            raise OSError("send failed")
        if "all-refused" in FAIL:
            raise SMTPRecipientsRefused({k: tuple(v) for k, v in REFUSE.items()})
        if "all-refused-malformed" in FAIL:
            # codes that cannot be read: the handler's own extraction fails
            raise SMTPRecipientsRefused({"operator@example.test": "550 operator@example.test"})
        if "sender-refused" in FAIL:
            raise SMTPSenderRefused(553, b"sender alerts@example.test not allowed",
                                    "alerts@example.test")
        if "code-interrupt" in FAIL:
            raise CodeInterrupted("message to operator@example.test")
        if "data-error" in FAIL:
            raise SMTPDataError(554, b"message to operator@example.test rejected")
        self.log["calls"].append(["send", {k: msg[k] for k in ("Subject", "From", "To", "Cc")},
                                  msg.get_content()])
        return {k: tuple(v) for k, v in REFUSE.items()}
'''

FACTS = {"WORKFLOW": "ercot-tick", "STATUS": "failure", "EVENT": "schedule", "DRILL": "false",
         "RUN_ID": "5", "ATTEMPT": "2", "RUN_URL": "https://example.test/actions/runs/5/attempts/2",
         "COMMIT": "abc123"}
SECRETS = {"ALERT_SMTP_USER": "alerts@example.test", "ALERT_SMTP_PASSWORD": "pw",
           "ALERT_EMAIL_TO": "operator@example.test"}


def _run(tmp_path: Path, env: dict[str, str]):
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "smtplib.py").write_text(FAKE_SMTPLIB)
    log = tmp_path / "smtp.log"
    full = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(fake),
            "FAKE_SMTP_LOG": str(log), **env}
    done = subprocess.run([sys.executable, "-"], input=_script(_workflow("ercot.yml")), env=full,
                          capture_output=True, text=True, timeout=60)
    sent = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return done, sent


def _message(sent):
    sends = [c for c in sent[0]["calls"] if isinstance(c, list) and c[0] == "send"]
    assert len(sends) == 1
    return sends[0][1], sends[0][2]


def test_a_failed_run_sends_one_email_naming_the_run_and_the_attempt(tmp_path):
    done, sent = _run(tmp_path, {**FACTS, **SECRETS})
    assert done.returncode == 0, done.stderr
    assert len(sent) == 1
    assert (sent[0]["host"], sent[0]["port"], sent[0]["timeout"]) == ("smtp.gmail.com", 587, 30)
    assert sent[0]["calls"][:2] == ["starttls", ["login", "alerts@example.test", "pw"]]
    # register T-42: the server's certificate and name are verified before the login
    assert sent[0]["tls"] == {"is_context": True, "check_hostname": True, "verify_required": True}
    headers, body = _message(sent)
    assert headers == {"Subject": "talea ercot-tick FAILURE: run 5 attempt 2",
                       "From": "alerts@example.test", "To": "operator@example.test",
                       "Cc": "alerts@example.test"}
    assert "ercot-tick, run 5, attempt 2: FAILURE (started by schedule)." in body
    assert "Run: https://example.test/actions/runs/5/attempts/2" in body
    assert "Commit: abc123" in body
    assert "may exist only on its runner" in body and "the day is missed" in body
    assert "drill" not in body.lower()
    assert "email sent" in done.stdout


def test_every_fact_reaches_the_email(tmp_path):
    facts = {"WORKFLOW": "jp-tick", "STATUS": "failure", "EVENT": "workflow_dispatch",
             "DRILL": "false", "RUN_ID": "987", "ATTEMPT": "3",
             "RUN_URL": "https://example.test/r/987/attempts/3", "COMMIT": "fedcba"}
    done, sent = _run(tmp_path, {**facts, **SECRETS})
    assert done.returncode == 0, done.stderr
    headers, body = _message(sent)
    assert headers["Subject"] == "talea jp-tick FAILURE: run 987 attempt 3"
    for fact in ("jp-tick, run 987, attempt 3", "started by workflow_dispatch",
                 "https://example.test/r/987/attempts/3", "Commit: fedcba"):
        assert fact in body


def test_a_cancelled_run_says_cancelled_and_what_that_may_mean(tmp_path):
    done, sent = _run(tmp_path, {**FACTS, **SECRETS, "STATUS": "cancelled"})
    assert done.returncode == 0, done.stderr
    headers, body = _message(sent)
    assert headers["Subject"] == "talea ercot-tick CANCELLED: run 5 attempt 2"
    assert "a newer run replaced it, this run produced no receipt" in body
    assert "nothing was lost" not in body
    assert "cancelled after it started" in body


def test_a_drill_says_drill_and_that_nothing_was_committed(tmp_path):
    done, sent = _run(tmp_path, {**FACTS, **SECRETS, "DRILL": "true", "EVENT": "workflow_dispatch"})
    assert done.returncode == 0, done.stderr
    headers, body = _message(sent)
    assert headers["Subject"] == "talea ercot-tick DRILL FAILURE: run 5 attempt 2"
    assert "This was a drill" in body and "Nothing was fetched or committed." in body
    assert "may exist only on its runner" not in body


@pytest.mark.parametrize("missing", sorted(SECRETS))
def test_a_missing_secret_fails_loudly_and_sends_nothing(tmp_path, missing):
    env = {**FACTS, **{k: v for k, v in SECRETS.items() if k != missing}}
    done, sent = _run(tmp_path, env)
    assert done.returncode == 1
    assert "EMAIL-NOT-CONFIGURED" in done.stderr
    assert sent == []


def test_an_empty_secret_counts_as_missing(tmp_path):
    # GitHub hands an unset secret to a step as an empty string.
    done, sent = _run(tmp_path, {**FACTS, **SECRETS, "ALERT_EMAIL_TO": ""})
    assert done.returncode == 1 and sent == []


@pytest.mark.parametrize("stage, message", [
    ("connect", "connection refused"), ("starttls", "starttls failed"),
    ("login", "authentication failed"), ("send", "send failed"), ("quit", "quit failed")])
def test_an_smtp_error_at_any_stage_fails_the_step_with_its_type_only(tmp_path, stage, message):
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_FAIL": stage})
    assert done.returncode == 1
    assert "EMAIL-FAILED: OSError (SMTP codes [])" in done.stderr
    assert message not in done.stderr, "an exception's text is never printed"
    assert "Traceback" not in done.stderr
    assert "email sent" not in done.stdout


@pytest.mark.parametrize("fail, line", [
    # every recipient refused, then QUIT fails on the way out: the cleanup
    # exception would chain the refusal (and its addresses) into a traceback
    ("all-refused,quit", "EMAIL-FAILED: OSError (SMTP codes [])"),
    ("sender-refused", "EMAIL-FAILED: SMTPSenderRefused (SMTP codes [553])"),
    ("data-error", "EMAIL-FAILED: SMTPDataError (SMTP codes [554])"),
    # every recipient refused, then the SIGINT GitHub sends on a cancel lands
    # during QUIT: KeyboardInterrupt is not an Exception
    ("all-refused,quit-interrupt", "EMAIL-FAILED: KeyboardInterrupt (SMTP codes [])"),
    # the handler's own code extraction fails on an unreadable payload
    ("all-refused-malformed", "EMAIL-FAILED: SMTPRecipientsRefused (SMTP codes [])"),
    # a KeyboardInterrupt while the handler reads the code: the handler's own
    # guard must be BaseException too, not Exception (review suggestion on the
    # merged notify job: narrowing that inner guard went undetected)
    ("code-interrupt", "EMAIL-FAILED: CodeInterrupted (SMTP codes [])"),
])
def test_no_failure_prints_an_address_or_a_traceback(tmp_path, fail, line):
    refused = json.dumps({"operator@example.test": [550, "no such user"]})
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_FAIL": fail,
                              "FAKE_SMTP_REFUSE": refused})
    assert done.returncode == 1
    assert line in done.stderr
    assert "Traceback" not in done.stderr
    assert "example.test" not in done.stderr + done.stdout
    assert "email sent" not in done.stdout


def test_a_refused_operator_address_fails_even_when_the_copy_is_accepted(tmp_path):
    refused = json.dumps({"operator@example.test": [550, "no such user"]})
    done, sent = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_REFUSE": refused})
    assert done.returncode == 1
    assert "EMAIL-REFUSED" in done.stderr and "550" in done.stderr
    assert "email sent" not in done.stdout
    # the log names codes, never an address
    assert "operator@example.test" not in done.stderr + done.stdout


def test_every_recipient_refused_fails_with_codes_only(tmp_path):
    """smtplib raises SMTPRecipientsRefused, carrying {address: (code, msg)},
    when the server refuses every recipient. Its traceback would print the
    addresses; it is caught and only the codes are printed."""
    refused = json.dumps({"operator@example.test": [550, "no such user"],
                          "alerts@example.test": [550, "no such user"]})
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_FAIL": "all-refused",
                              "FAKE_SMTP_REFUSE": refused})
    assert done.returncode == 1
    assert "EMAIL-FAILED: SMTPRecipientsRefused (SMTP codes [550, 550])" in done.stderr
    assert "Traceback" not in done.stderr
    assert "example.test" not in done.stderr + done.stdout
    assert "email sent" not in done.stdout


def test_a_malformed_secret_in_a_header_fails_with_its_type_only(tmp_path):
    """Building the message is inside the same handler: a secret holding a line
    break makes the header assignment raise, and its text is never printed."""
    done, sent = _run(tmp_path, {**FACTS, **SECRETS,
                                 "ALERT_EMAIL_TO": "operator@example.test\nBcc: x@example.test"})
    assert done.returncode == 1
    assert "EMAIL-FAILED: ValueError (SMTP codes [])" in done.stderr
    assert "Traceback" not in done.stderr
    assert "example.test" not in done.stderr + done.stdout
    assert sent == []


def test_the_subject_line_survives_the_hard_exit(tmp_path):
    """os._exit skips Python's cleanup, so the subject printed before the
    exchange is flushed first and still reaches the run's log."""
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_FAIL": "login"})
    assert done.returncode == 1
    assert "talea ercot-tick FAILURE: run 5 attempt 2" in done.stdout


def test_a_refused_operator_address_written_with_a_name_still_fails(tmp_path):
    refused = json.dumps({"operator@example.test": [550, "no such user"]})
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_REFUSE": refused,
                              "ALERT_EMAIL_TO": "The Operator <operator@example.test>"})
    assert done.returncode == 1 and "EMAIL-REFUSED" in done.stderr


def test_a_refused_copy_alone_is_reported_but_the_email_counts_as_sent(tmp_path):
    refused = json.dumps({"alerts@example.test": [452, "mailbox full"]})
    done, _ = _run(tmp_path, {**FACTS, **SECRETS, "FAKE_SMTP_REFUSE": refused})
    assert done.returncode == 0, done.stderr
    assert "refused the copy only" in done.stdout and "452" in done.stdout
    assert "alerts@example.test" not in done.stdout
    assert "email sent" in done.stdout
