"""CLI-shell coverage for __main__: the status command, arg dispatch, the git
backup outcomes, summary printing, and the .env fallback. These are the
effectful edges the loop tests don't reach; each pins a branch that would
otherwise fail silently in production (a mis-dispatched command, a swallowed
push failure, a missing crosscheck alert)."""
import json
import sys
from types import SimpleNamespace

import pytest

import talea.__main__ as cli
from talea import loop

V = "1"


def _seed(monkeypatch, tmp_path, ledger=(), receipts=()):
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    (tmp_path / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ledger))
    (tmp_path / "receipts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts))
    return tmp_path


# ---- cmd_status --------------------------------------------------------------

def test_status_empty_ledger(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path)
    assert cli.cmd_status() == 0
    out = capsys.readouterr().out
    assert "receipts: 0 total, 0 open" in out
    assert "ledger: empty" in out


def test_status_summarizes_per_strategy_and_open_receipts(monkeypatch, tmp_path, capsys):
    led = [{"target": "2026-08-13", "strategy": loop.STRATEGY,
            "strategy_version": V, "pnl_eur": 100.0, "capture": 0.95},
           {"target": "2026-08-14", "strategy": loop.STRATEGY,
            "strategy_version": V, "pnl_eur": -20.0, "capture": 0.80}]
    rec = led + [{"target": "2026-08-15", "strategy": loop.STRATEGY,
                  "strategy_version": V}]      # unsettled -> 1 open
    _seed(monkeypatch, tmp_path, ledger=led, receipts=rec)
    assert cli.cmd_status() == 0
    out = capsys.readouterr().out
    assert "receipts: 3 total, 1 open" in out
    assert f"{loop.STRATEGY} v{V}: 2 settled | total +80.00 EUR" in out
    assert "win rate 50%" in out and "mean capture 88%" in out


def test_status_flags_crosscheck_alerts(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path,
          ledger=[{"target": "2026-08-13", "strategy": loop.STRATEGY,
                   "strategy_version": V, "pnl_eur": 1.0, "capture": 0.9}])
    (tmp_path / "CROSSCHECK-ALERTS.log").write_text("2026-08-13 disagreed\n")
    cli.cmd_status()
    assert "CROSSCHECK ALERTS present" in capsys.readouterr().out


# ---- main() dispatch ---------------------------------------------------------

@pytest.fixture
def stub_cmds(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_tick", lambda m=None: calls.append(("tick", m)) or 0)
    monkeypatch.setattr(cli, "cmd_status", lambda: calls.append(("status",)) or 0)
    return calls


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["esios-paper", *argv])
    return cli.main()


def test_main_defaults_to_tick(monkeypatch, stub_cmds):
    assert _run_main(monkeypatch, []) == 0
    assert stub_cmds == [("tick", None)]


def test_main_status(monkeypatch, stub_cmds):
    assert _run_main(monkeypatch, ["status"]) == 0
    assert stub_cmds == [("status",)]


def test_main_tick_with_market(monkeypatch, stub_cmds):
    assert _run_main(monkeypatch, ["tick", "--market", "de"]) == 0
    assert stub_cmds == [("tick", "de")]


def test_main_market_flag_without_value_is_none(monkeypatch, stub_cmds):
    assert _run_main(monkeypatch, ["tick", "--market"]) == 0
    assert stub_cmds == [("tick", None)]


def test_main_unknown_command(monkeypatch, stub_cmds, capsys):
    assert _run_main(monkeypatch, ["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().out
    assert stub_cmds == []


# ---- git_backup (all four outcomes) ------------------------------------------

class _FakeGit:
    """Stand-in for subprocess.run over `git -C REPO <sub> ...`."""
    def __init__(self, diff=1, commit=0, push=0):
        self.rc = {"add": 0, "diff": diff, "commit": commit, "push": push}
        self.seen = []
    def __call__(self, cmd, **kw):
        sub = cmd[3]
        self.seen.append(sub)
        return SimpleNamespace(returncode=self.rc[sub], stderr="e", stdout="o")


def _backup(monkeypatch, fake, capsys):
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli.git_backup("2026-08-27")
    return capsys.readouterr().out


def test_git_backup_up_to_date(monkeypatch, capsys):
    out = _backup(monkeypatch, _FakeGit(diff=0), capsys)   # no staged changes
    assert "up to date" in out


def test_git_backup_commit_and_push(monkeypatch, capsys):
    fake = _FakeGit(diff=1, commit=0, push=0)
    out = _backup(monkeypatch, fake, capsys)
    assert "pushed" in out and "commit" in fake.seen and "push" in fake.seen


def test_git_backup_commit_failure_does_not_push(monkeypatch, capsys):
    fake = _FakeGit(diff=1, commit=1)
    out = _backup(monkeypatch, fake, capsys)
    assert "COMMIT-FAILED" in out and "push" not in fake.seen


def test_git_backup_push_failure_is_logged_not_raised(monkeypatch, capsys):
    out = _backup(monkeypatch, _FakeGit(diff=1, commit=0, push=1), capsys)
    assert "PUSH-FAILED" in out and "ride next" in out


# ---- _print_summary / _env / email no-config ---------------------------------

def test_print_summary_covers_every_line(capsys):
    cli._print_summary({
        "fetch_error": "timeout",
        "settled": [{"target": "2026-08-13", "strategy": loop.STRATEGY,
                     "strategy_version": V, "pnl_eur": 10.0,
                     "oracle_pnl_eur": 12.0, "capture": 0.83, "tau": 0.9}],
        "committed": [{"target": "2026-08-14", "strategy": loop.STRATEGY,
                       "strategy_version": V, "buy_hours": [1, 2],
                       "sell_hours": [20, 21], "basis_day": "2026-08-13"}],
        "skipped": ["already committed (idempotent)"],
    }, "esios-paper")
    out = capsys.readouterr().out
    assert "FETCH-ERROR timeout" in out and "SETTLED 2026-08-13" in out
    assert "COMMITTED 2026-08-14" in out and "SKIPPED commit" in out


def test_env_reads_dotenv_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "REPO", tmp_path)
    (tmp_path / ".env").write_text('OTHER=x\nESIOS_TESTKEY=secret-value\n')
    monkeypatch.delenv("ESIOS_TESTKEY", raising=False)
    assert cli._env("ESIOS_TESTKEY") == "secret-value"
    assert cli._env("ABSENT_KEY") is None


def test_email_digest_silent_without_config(monkeypatch):
    monkeypatch.setattr(cli, "_env", lambda k: None)   # no TO / PASSWORD
    # must return without attempting SMTP and without raising
    cli.email_digest({"settled": [{"target": "t"}]})


def test_build_digest_none_on_empty_ledger(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    assert cli.build_digest() is None


def test_build_digest_crosscheck_alert_and_shadow_without_primary(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, ledger=[
        {"target": "2026-08-13", "strategy": loop.STRATEGY, "strategy_version": V,
         "pnl_eur": 10.0, "capture": 0.9, "tau": 0.9}])
    (tmp_path / "CROSSCHECK-ALERTS.log").write_text("routes disagreed\n")
    (tmp_path / "de").mkdir()               # shadow ledger with NO primary rows
    (tmp_path / "de" / "ledger.jsonl").write_text(json.dumps({
        "target": "2026-08-13", "strategy": "battery-2h2h-climatology",
        "strategy_version": V, "pnl_eur": 5.0, "capture": 0.9}) + "\n")
    subject, body = cli.build_digest()
    assert "CROSSCHECK-ALERTS.log present" in body and subject.startswith("ALERT")
    assert "[DE public]" not in body        # skipped: no primary strategy rows


class _FakeSMTP:
    sent: dict = {}
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, *a): pass
    def send_message(self, msg):
        _FakeSMTP.sent = {"subject": msg["Subject"], "body": msg.get_content()}


def test_email_digest_returns_when_build_is_none(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)                 # empty ledger -> build None
    monkeypatch.setattr(cli, "_env", lambda k: "configured")
    def _boom(*a, **k):
        raise AssertionError("SMTP must not be reached when digest is None")
    cli.email_digest({"settled": [{"target": "t"}]}, _smtp=_boom)


def test_email_digest_trouble_prepends_fetch_error(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, ledger=[
        {"target": "2026-08-13", "strategy": loop.STRATEGY, "strategy_version": V,
         "pnl_eur": 10.0, "capture": 0.9, "tau": 0.9}])
    monkeypatch.setattr(cli, "_env", lambda k: "configured")
    _FakeSMTP.sent = {}
    cli.email_digest({"settled": [{}], "fetch_error": "boom"}, _smtp=_FakeSMTP)
    assert "fetch_error: boom" in _FakeSMTP.sent["body"]
    assert _FakeSMTP.sent["subject"].startswith("ALERT")


def test_ots_stamp_slot_overflow(monkeypatch, tmp_path, capsys):
    ots = tmp_path / "ots"; ots.mkdir()
    rec = tmp_path / "r.jsonl"; rec.write_text("current receipts\n")
    led = tmp_path / "l.jsonl"; led.write_text("current ledger\n")
    for n in range(1, 25):                       # all 24 slots stamped, stale
        m = ots / (f"L.txt" if n == 1 else f"L-{n}.txt")
        m.write_text("OLD STATE")
        m.with_suffix(".txt.ots").write_bytes(b"proof")
    called = []
    cli.ots_stamp("L", ots_dir=ots, receipts=rec, ledger=led,
                  _run=lambda *a, **k: called.append(1))
    assert "slot overflow" in capsys.readouterr().out and not called


def test_markets_command_by_driver(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["esios-paper", "markets", "--driver", "server"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == "de it pt fr gb"


def test_markets_command_public(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["esios-paper", "markets", "--public"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == "es de gb ercot"


def test_markets_command_all(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["esios-paper", "markets"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == "es de it pt fr gb jp ercot"


# ---- Pillar B in the digest (incident 2026-10-01) -----------------------------

def _dark_market(tmp_path, slug, days_dark):
    """A registered market whose newest price and newest receipt are `days_dark`
    days old — exactly how IT/PT/FR looked for 33 days."""
    from datetime import datetime, timedelta, timezone
    d = tmp_path / slug
    d.mkdir()
    then = datetime(2026, 10, 1, 12, tzinfo=timezone.utc) - timedelta(days=days_dark)
    d.joinpath("prices.json").write_text(json.dumps(
        [{"ts": then.strftime("%Y-%m-%dT%H"), "price": 50.0}]))
    d.joinpath("receipts.jsonl").write_text(json.dumps(
        {"target": "x", "committed_at": then.isoformat()}) + "\n")


def test_build_digest_flags_a_dark_registered_market(monkeypatch, tmp_path):
    """The bug class: a shadow with a broken fetch kept its last settled line in
    every digest, unchanged, with no alert — the only liveness signal was an
    ephemeral phone push. Now the SAME check the server CLI runs heads ALERTS."""
    from datetime import datetime, timezone
    _seed(monkeypatch, tmp_path, ledger=[
        {"target": "2026-09-30", "strategy": loop.STRATEGY, "strategy_version": V,
         "pnl_eur": 10.0, "capture": 0.9, "tau": 0.9}])
    _dark_market(tmp_path, "it", days_dark=33)
    _dark_market(tmp_path, "de", days_dark=0)          # healthy control
    now = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
    subject, body = cli.build_digest(now=now)
    assert subject.startswith("ALERT")
    assert body.startswith("ALERTS: LIVENESS STALE: it (")
    assert "de" not in body.split("\n")[0]              # healthy market not named
    assert "STALE    it" in body and "33d stale" in body


def test_build_digest_no_liveness_alert_when_all_live_or_onboarding(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    _seed(monkeypatch, tmp_path, ledger=[
        {"target": "2026-09-30", "strategy": loop.STRATEGY, "strategy_version": V,
         "pnl_eur": 10.0, "capture": 0.9, "tau": 0.9}])
    _dark_market(tmp_path, "de", days_dark=1)           # live; others NEVER (no files)
    subject, body = cli.build_digest(now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
    assert not subject.startswith("ALERT") and "LIVENESS" not in body


def test_liveness_rows_cover_every_registered_market(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from talea.markets import MARKETS
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    rows = cli._liveness_rows(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [r["slug"] for r in rows] == list(MARKETS)
    assert {r["state"] for r in rows} == {"NEVER"}      # empty tree: nothing stale
