"""The publisher's private-content gate (register D-37), outside the publisher
script itself (its behaviour is tested in tests/test_mirror_tree.py):

- the trust anchor the tick hands it: MIRROR_BASE, main as the tick fetched it,
  taken before any tick commits;
- the alert command a refusal sends through (`python -m talea alert`);
- the one render time per publish (TALEA_RENDER_EPOCH), without which the
  check's rebuild of the tree would differ from the build whenever the minute
  turned between them, and the gate would refuse an honest publish;
- the real check on a really built tree, once with the publish's render time
  and once without it."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import talea.__main__ as cli
from conftest import private_file
from test_git_divergence import _diverged, _git, _write

ROOT = Path(__file__).resolve().parents[1]
TICK = ROOT / "scripts" / "server_tick.sh"


# ---- the trust anchor: MIRROR_BASE ----

def _anchor_block() -> str:
    """The pre-tick pull and the anchor, verbatim from scripts/server_tick.sh."""
    text = TICK.read_text()
    m = re.search(r"^git pull --ff-only.*?^export MIRROR_BASE\n", text, re.M | re.S)
    assert m, "server_tick.sh lost its pull or its MIRROR_BASE"
    return m.group(0)


def _base_after(server: Path) -> tuple[str, str]:
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + _anchor_block() + 'printf "\\nBASE=%s" "$MIRROR_BASE"'],
                       cwd=server, capture_output=True, text=True)
    return r.stdout.rsplit("BASE=", 1)[1], r.stdout


def test_the_base_is_main_as_the_tick_fetched_it_not_the_servers_own_commits(tmp_path):
    """The server has an unpushed data commit and the Actions writer has pushed:
    the base is origin's main as fetched (the Actions commit), never a commit
    only the server holds."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    _git(server, "commit", "-qam", "data: tick (unpushed)")
    base, _ = _base_after(server)
    assert base == _git(origin, "rev-parse", "main").strip()
    assert base != _git(server, "rev-parse", "HEAD").strip()


def test_with_no_origin_main_the_base_is_empty_and_the_journal_says_the_gate_cannot_judge(tmp_path):
    """The line is true in either mode: it promises no refusal (observe mode
    publishes without a base), only that the gate cannot judge and that the
    publisher emails."""
    lone = tmp_path / "lone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(lone)], check=True)
    base, out = _base_after(lone)
    assert base == "" and "the gate cannot judge the publish: the publisher emails" in out
    assert "refuse" not in out


def test_the_base_is_taken_before_any_tick_and_reaches_the_publisher():
    text = "\n".join(l for l in TICK.read_text().splitlines() if not l.lstrip().startswith("#"))
    anchor = text.index("MIRROR_BASE=\"$(git rev-parse --verify --quiet 'refs/remotes/origin/main^{commit}')\"")
    assert text.index("git pull --ff-only") < anchor < text.index("export MIRROR_BASE")
    assert text.index("export MIRROR_BASE") < text.index("python -m talea tick")
    assert text.index("export MIRROR_BASE") < text.index("scripts/publish_mirror.sh")


# ---- the alert command ----

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
            raise OSError("smtp down: secret-detail")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset():
    FakeSMTP.sent, FakeSMTP.fail = [], False


def _configured(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "REPO", tmp_path)            # no .env there
    monkeypatch.setenv("ALERT_EMAIL_TO", "operator@example.test")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("ALERT_SMTP_USER", "alerts@example.test")


def test_an_alert_is_sent_from_the_alert_account_to_the_operator_with_a_copy(monkeypatch, tmp_path, capsys):
    _configured(monkeypatch, tmp_path)
    assert cli.email_alert("the subject", "the body", _smtp=FakeSMTP) is True
    msg, = FakeSMTP.sent
    assert (msg["From"], msg["To"], msg["Cc"]) == ("alerts@example.test", "operator@example.test",
                                                     "alerts@example.test")
    assert msg["Subject"] == "the subject" and msg.get_content() == "the body\n"
    assert "alert emailed" in capsys.readouterr().out


def test_an_alert_that_cannot_be_sent_says_so_and_never_raises(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "REPO", tmp_path)
    for k in ("ALERT_EMAIL_TO", "ALERT_SMTP_PASSWORD", "ALERT_SMTP_USER"):
        monkeypatch.delenv(k, raising=False)
    assert cli.email_alert("s", "b", _smtp=FakeSMTP) is False
    assert "NOT sent" in capsys.readouterr().out and FakeSMTP.sent == []
    _configured(monkeypatch, tmp_path)
    FakeSMTP.fail = True
    assert cli.email_alert("s", "b", _smtp=FakeSMTP) is False
    out = capsys.readouterr().out
    assert "alert email FAILED: OSError" in out and "secret-detail" not in out


class PartlyRefusing(FakeSMTP):
    refuse: dict = {}

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)
        return dict(PartlyRefusing.refuse)


def test_a_refused_operator_address_is_a_failure_and_a_refused_copy_is_only_reported(
        monkeypatch, tmp_path, capsys):
    """send_message can accept the alert account's copy and refuse the
    operator: that must not count as sent (the writers' rule)."""
    _configured(monkeypatch, tmp_path)
    PartlyRefusing.refuse = {"operator@example.test": (550, b"no such user")}
    assert cli.email_alert("s", "b", _smtp=PartlyRefusing) is False
    out = capsys.readouterr().out
    assert "alert email FAILED: RecipientRefused" in out and "no such user" not in out
    PartlyRefusing.refuse = {"alerts@example.test": (452, b"mailbox full")}
    assert cli.email_alert("s", "b", _smtp=PartlyRefusing) is True
    out = capsys.readouterr().out
    assert "refused the copy only (SMTP codes [452])" in out and "mailbox full" not in out
    monkeypatch.setenv("ALERT_EMAIL_TO", "not an address <>")
    PartlyRefusing.refuse = {}
    assert cli.email_alert("s", "b", _smtp=PartlyRefusing) is False


def test_the_digest_shares_the_rule(monkeypatch, tmp_path, capsys):
    _configured(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "build_digest", lambda: ("subject", "body"))
    PartlyRefusing.refuse = {"operator@example.test": (550, b"no")}
    cli.email_digest({"settled": [{"x": 1}], "primary_receipt_stands": True}, _smtp=PartlyRefusing)
    assert "digest email FAILED" in capsys.readouterr().out


def test_the_alert_command_reads_its_body_file_and_answers_with_its_exit(tmp_path, monkeypatch):
    body = tmp_path / "body.txt"
    body.write_text("The public mirror was NOT published: x.\n")
    seen = []
    assert cli.cmd_alert(["--subject", "S", "--body-file", str(body)],
                         _send=lambda s, b: seen.append((s, b)) or True) == 0
    assert seen == [("S", "The public mirror was NOT published: x.\n")]
    assert cli.cmd_alert(["--subject", "S", "--body-file", str(body)], _send=lambda s, b: False) == 1
    for bad in ([], ["--subject"], ["--subject", "S"], ["--subject", "S", "--body-file", str(tmp_path / "no")]):
        assert cli.cmd_alert(bad, _send=lambda s, b: True) == 2
    monkeypatch.setattr(sys, "argv", ["talea", "alert", "--subject", "S", "--body-file", str(body)])
    monkeypatch.setattr(cli, "email_alert", lambda s, b: True)
    assert cli.main() == 0


def test_the_alert_body_can_come_through_a_pipe(tmp_path):
    """The publisher pipes the body (`--body-file /dev/stdin`), so that no
    temporary file stands between a refusal and its email."""
    r, w = os.pipe()
    os.write(w, b"The public mirror was NOT published: piped.\n")
    os.close(w)
    seen = []
    try:
        assert cli.cmd_alert(["--subject", "S", "--body-file", f"/dev/fd/{r}"],
                             _send=lambda s, b: seen.append(b) or True) == 0
    finally:
        os.close(r)
    assert seen == ["The public mirror was NOT published: piped.\n"]


# ---- one render time per publish ----

def _renderer():
    spec = importlib.util.spec_from_file_location("render_dashboard", ROOT / "scripts" / "render_dashboard.py")
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)
    return rd


def test_the_render_time_is_the_publishs_when_it_is_given(monkeypatch):
    rd = _renderer()
    monkeypatch.delenv("TALEA_RENDER_EPOCH", raising=False)
    assert abs(rd._now() - datetime.now(timezone.utc)) < timedelta(seconds=5)   # unset: the clock
    at = datetime(2026, 11, 8, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("TALEA_RENDER_EPOCH", str(int(at.timestamp())))
    assert rd._now() == at
    assert rd._now().tzinfo is not None and str(rd._now().tzinfo) == "Europe/Madrid"
    first = rd.build("es")
    monkeypatch.setattr(rd, "datetime", _LaterClock)                # the wall clock moves on
    assert rd.build("es") == first
    monkeypatch.delenv("TALEA_RENDER_EPOCH")
    assert rd.build("es") != first                                    # and without it, the page moves


class _LaterClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(minutes=3)


@pytest.mark.parametrize("raw", ["", "abc", "-5", "1.5", "１２３", " 12"])
def test_a_render_time_that_is_not_whole_seconds_is_an_error(monkeypatch, raw):
    rd = _renderer()
    monkeypatch.setenv("TALEA_RENDER_EPOCH", raw)
    with pytest.raises(ValueError, match="TALEA_RENDER_EPOCH"):
        rd._now()


# ---- the real check on a really built tree ----

EPOCH = str(int(datetime(2026, 11, 16, 10, 1, tzinfo=timezone.utc).timestamp()))


def _build(into: Path, epoch: str) -> None:
    env = {**os.environ, "TALEA_RENDER_EPOCH": epoch}
    r = subprocess.run(["bash", str(ROOT / "scripts" / "mirror_tree.sh"), str(into)], cwd=ROOT,
                       capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stderr[-2000:]


def _check(tree: Path, epoch: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TALEA_RENDER_EPOCH": epoch}
    return subprocess.run([sys.executable, "-I", "-S", str(private_file("team/private_content_check.py")),
                           "--repo", str(ROOT), "--tree", str(tree), "--base", "HEAD"],
                          cwd=ROOT, capture_output=True, text=True, timeout=900, env=env)


def test_the_real_check_proves_a_built_tree_only_with_the_same_render_time(tmp_path):
    """What the gate runs on the server, here: the builder, then the check with
    --tree. With the publish's render time the check's own rebuild equals the
    tree, so an honest publish passes; with another minute it cannot prove the
    tree and refuses (exit 2). The pinned time is what makes the gate usable."""
    tree = tmp_path / "tree"
    _build(tree, EPOCH)
    same = _check(tree, EPOCH)
    assert same.returncode == 0, same.stdout[-2000:]
    other = _check(tree, str(int(EPOCH) + 60))                          # one minute later
    assert other.returncode == 2 and "is not the one scripts/mirror_tree.sh builds" in other.stdout


# ---- what the gate must not ship without ----

def test_the_gate_ships_only_with_option_a_and_the_freshness_detector():
    """MERGE ORDER, enforced. Without option (A) in the check (a private run
    that a public market publishes at the same instants is public), the gate
    refuses ordinary ticks: Spain's new hours equal Portugal's. Without the
    mirror-freshness detector, a refusal has only the gate's own email: the
    second, independent channel the team required before the gate goes on the
    server. This branch's CI stays red until both are on main and merged in."""
    check = private_file("team/private_content_check.py").read_text()
    assert "def aligned_public_runs(" in check, \
        "option (A) is not in team/private_content_check.py: merge it before the gate"
    assert (ROOT / "scripts" / "check_mirror_freshness.py").exists(), \
        "the mirror-freshness detector is not here: merge it before the gate"
