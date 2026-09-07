"""The independent verifier must PASS on a faithful ledger and FAIL on a
tampered one — a re-derivation that cannot fail proves nothing. Each test
mutates one thing a real bug (or bad actor) would and asserts detection."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

# Load scripts/verify_ledger.py directly (it's a script, not a package module).
_SPEC = importlib.util.spec_from_file_location(
    "verify_ledger", Path(__file__).resolve().parents[1] / "scripts" / "verify_ledger.py")
vl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vl)

PARAMS = {"power_mw": 1.0, "rt_eff": 0.85, "fee_eur_mwh": 0.5}
TARGET, BASIS = "2026-01-02", "2026-01-01"


def _day_prices():
    # 24 distinct hourly prices; cheapest hours 0,1 / dearest 22,23.
    return [{"ts": f"{TARGET}T{h:02d}:00:00", "price": 100.0 + h} for h in range(24)]


def _write(base: Path, receipts, ledger, prices=None):
    (base / "prices.json").write_text(json.dumps(prices or _day_prices()))
    (base / "receipts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts))
    (base / "ledger.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in ledger))


def _faithful(base: Path):
    """A receipt + its correctly-derived settlement (buy off-oracle so capture<1)."""
    actual = {h: 100.0 + h for h in range(24)}
    buy, sell = [5, 6], [22, 23]
    rec = {"target": TARGET, "basis_day": BASIS, "buy_hours": buy,
           "sell_hours": sell, "strategy": "battery-2h2h-persistence",
           "strategy_version": "1", "params": PARAMS,
           "committed_at": f"{BASIS}T09:00:00+00:00"}
    ob, os_ = vl.pick_extremes(actual, 2)
    p = vl.pnl(buy, sell, actual, PARAMS)
    orc = vl.pnl(ob, os_, actual, PARAMS)
    entry = {"target": TARGET, "strategy": rec["strategy"],
             "strategy_version": "1", "buy_hours": buy, "sell_hours": sell,
             "buy_prices": [actual[h] for h in buy],
             "sell_prices": [actual[h] for h in sell], "pnl_eur": p,
             "oracle_pnl_eur": orc, "capture": round(p / orc, 3)}
    _write(base, [rec], [entry])
    return rec, entry


@pytest.fixture
def market(tmp_path, monkeypatch):
    # ES lives at Data/es/ now (Stage B) — verify_market("es") reads DATA/es, so
    # seed the es subdir and keep vl.DATA as its parent.
    monkeypatch.setattr(vl, "DATA", tmp_path)
    es = tmp_path / "es"
    es.mkdir()
    return es


def test_faithful_ledger_passes(market):
    _faithful(market)
    rep = vl.verify_market("es", verify_ots=False)
    assert rep.fails == [], rep.fails
    assert rep.checked == 1


def test_tampered_pnl_is_caught(market):
    _, entry = _faithful(market)
    entry["pnl_eur"] = round(entry["pnl_eur"] + 5.0, 2)  # inflate reported P&L
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("pnl re-derived" in f for f in rep.fails), rep.fails


def test_altered_hours_are_caught(market):
    _, entry = _faithful(market)
    entry["sell_hours"] = [21, 23]  # settle on hours the receipt never committed
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("differ from the committed receipt" in f for f in rep.fails), rep.fails


def test_leak_committed_on_target_day_is_caught(market):
    rec, entry = _faithful(market)
    rec["committed_at"] = f"{TARGET}T09:00:00+00:00"  # committed ON the target day
    (market / "receipts.jsonl").write_text(json.dumps(rec) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("LEAK" in f for f in rep.fails), rep.fails


def test_orphan_settlement_is_caught(market):
    _, entry = _faithful(market)
    (market / "receipts.jsonl").write_text("")  # settlement with no receipt behind it
    rep = vl.verify_market("es", verify_ots=False)
    assert any("orphan settlement" in f for f in rep.fails), rep.fails


def test_rewritten_history_breaks_ots_coverage(market):
    rec, entry = _faithful(market)
    ots = market / "ots"
    ots.mkdir()
    # An anchored manifest whose recorded hash matches NO prefix of the file =
    # the append-only trail was rewritten after anchoring.
    (ots / "2026-01-01.txt").write_text(
        "esios-paper audit manifest 2026-01-01\n"
        f"sha256(receipts.jsonl)={hashlib.sha256(b'a different history').hexdigest()}\n"
        "sha256(ledger.jsonl)=absent\n")
    (ots / "2026-01-01.txt.ots").write_bytes(b"\x00fake-proof")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("matches NO prefix" in f for f in rep.fails), rep.fails


def test_valid_ots_prefix_is_covered(market):
    _faithful(market)
    ots = market / "ots"
    ots.mkdir()
    digest = hashlib.sha256((market / "receipts.jsonl").read_bytes()).hexdigest()
    (ots / "2026-01-02.txt").write_text(
        "esios-paper audit manifest 2026-01-02\n"
        f"sha256(receipts.jsonl)={digest}\n"
        "sha256(ledger.jsonl)=absent\n")
    (ots / "2026-01-02.txt.ots").write_bytes(b"\x00proof")
    rep = vl.verify_market("es", verify_ots=False)
    assert rep.fails == [], rep.fails
    assert any("OTS-covered 1/1" in m for m in rep.info), rep.info


def test_default_report_never_claims_bitcoin_finality(market):
    """Audit finding 2026-09-07: without --verify-ots the checker only proves a
    .ots proof EXISTS and the manifest hashes a prefix of the file — it cannot
    tell a PENDING (calendar-only) proof from a Bitcoin-confirmed one, yet its
    default output said "manifests anchored" / "receipts Bitcoin-covered N/N"
    (on the day of the audit, receipts covered only by still-pending 09-06
    proofs were counted as "Bitcoin-covered"). The default report must say
    OTS-stamped/OTS-covered and point at --verify-ots for finality — the
    generated pages already had to learn this lesson (2026-08-29)."""
    _faithful(market)
    ots = market / "ots"
    ots.mkdir()
    digest = hashlib.sha256((market / "receipts.jsonl").read_bytes()).hexdigest()
    (ots / "2026-01-02.txt").write_text(
        "esios-paper audit manifest 2026-01-02\n"
        f"sha256(receipts.jsonl)={digest}\n"
        "sha256(ledger.jsonl)=absent\n")
    (ots / "2026-01-02.txt.ots").write_bytes(b"\x00pending-proof")
    rep = vl.verify_market("es", verify_ots=False)
    blob = "\n".join(rep.info + rep.warns)
    assert "Bitcoin-covered" not in blob and "anchored" not in blob, blob
    assert "OTS-stamped" in blob and "--verify-ots" in blob, blob


# ---- verify_market: the remaining fail/warn branches -------------------------

def test_recorded_prices_mismatch_is_caught(market):
    _, entry = _faithful(market)
    entry["buy_prices"] = [0.0, 0.0]            # not what prices.json says
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("prices don't match" in f for f in rep.fails), rep.fails


def test_oracle_mismatch_is_caught(market):
    _, entry = _faithful(market)
    entry["oracle_pnl_eur"] = round(entry["oracle_pnl_eur"] + 40.0, 2)
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("oracle re-derived" in f for f in rep.fails), rep.fails


def test_capture_mismatch_is_caught(market):
    _, entry = _faithful(market)
    entry["capture"] = 0.123
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("capture" in f for f in rep.fails), rep.fails


def test_basis_not_before_target_is_caught(market):
    rec, _ = _faithful(market)
    rec["basis_day"] = TARGET                   # basis == target
    (market / "receipts.jsonl").write_text(json.dumps(rec) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("not before target" in f for f in rep.fails), rep.fails


def test_settleable_but_unsettled_warns(market):
    rec, _ = _faithful(market)
    _write(market, [rec], [])                   # published day, no settlement
    rep = vl.verify_market("es", verify_ots=False)
    assert any("UNSETTLED" in w for w in rep.warns), rep.warns


def test_committed_after_basis_day_warns(market):
    prices = [{"ts": f"2026-01-05T{h:02d}:00:00", "price": 100.0 + h} for h in range(24)]
    rec = {"target": "2026-01-05", "basis_day": "2026-01-01",
           "buy_hours": [5, 6], "sell_hours": [22, 23],
           "strategy": "battery-2h2h-persistence", "strategy_version": "1",
           "params": PARAMS, "committed_at": "2026-01-03T09:00:00+00:00"}
    _write(market, [rec], [], prices=prices)
    rep = vl.verify_market("es", verify_ots=False)
    assert any("later than basis_day" in w for w in rep.warns), rep.warns


def test_ledger_hash_no_prefix_fails(market):
    _faithful(market)
    ots = market / "ots"; ots.mkdir()
    good = hashlib.sha256((market / "receipts.jsonl").read_bytes()).hexdigest()
    (ots / "m.txt").write_text(
        f"manifest\nsha256(receipts.jsonl)={good}\n"
        f"sha256(ledger.jsonl)={hashlib.sha256(b'foreign').hexdigest()}\n")
    (ots / "m.txt.ots").write_bytes(b"proof")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("ledger hash matches NO prefix" in f for f in rep.fails), rep.fails


def test_unanchored_manifest_warns(market):
    _faithful(market)
    ots = market / "ots"; ots.mkdir()
    (ots / "m.txt").write_text("manifest\n")    # no .txt.ots alongside
    rep = vl.verify_market("es", verify_ots=False)
    assert any("no OTS-stamped manifests" in w for w in rep.warns), rep.warns


def test_load_prices_accepts_dict_form(market):
    _, _ = _faithful(market)
    prices = {f"{TARGET}T{h:02d}:00:00": 100.0 + h for h in range(24)}
    (market / "prices.json").write_text(json.dumps(prices))   # dict, not list
    rep = vl.verify_market("es", verify_ots=False)
    assert rep.fails == [], rep.fails


# ---- main() + --verify-ots ---------------------------------------------------

def _anchored(market):
    ots = market / "ots"; ots.mkdir()
    dr = hashlib.sha256((market / "receipts.jsonl").read_bytes()).hexdigest()
    dl = hashlib.sha256((market / "ledger.jsonl").read_bytes()).hexdigest()
    (ots / "m.txt").write_text(
        f"manifest\nsha256(receipts.jsonl)={dr}\nsha256(ledger.jsonl)={dl}\n")
    (ots / "m.txt.ots").write_bytes(b"proof")


def test_main_clean_returns_zero(market, monkeypatch, capsys):
    _faithful(market)
    monkeypatch.setattr(vl.sys, "argv", ["verify_ledger.py", "--market", "es"])
    assert vl.main() == 0
    out = capsys.readouterr().out
    assert "ALL CHECKS PASSED" in out and "1 settlements re-derived" in out


def test_main_all_discovers_subdir_markets(market, monkeypatch, capsys):
    _faithful(market)                              # market = DATA/es
    de = market.parent / "de"; de.mkdir(); _faithful(de)   # sibling DATA/de
    monkeypatch.setattr(vl.sys, "argv", ["verify_ledger.py", "--all"])
    assert vl.main() == 0
    out = capsys.readouterr().out
    assert "=== ES" in out and "=== DE" in out


def test_main_discrepancy_returns_one(market, monkeypatch, capsys):
    _, entry = _faithful(market)
    entry["pnl_eur"] = round(entry["pnl_eur"] + 9.0, 2)
    (market / "ledger.jsonl").write_text(json.dumps(entry) + "\n")
    monkeypatch.setattr(vl.sys, "argv", ["verify_ledger.py", "--market", "es"])
    assert vl.main() == 1
    assert "DISCREPANCY FOUND" in capsys.readouterr().out


@pytest.mark.parametrize("blob,needle", [
    ("Success! Bitcoin block 800000 attests", "attested"),
    ("Pending: awaiting confirmation (not yet mined)", "pending"),
    ("Could not verify: calendar unreachable", "ots verify"),
])
def test_main_verify_ots_reports_status(market, monkeypatch, capsys, blob, needle):
    _faithful(market); _anchored(market)
    monkeypatch.setattr(vl.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": blob, "stderr": ""})())
    monkeypatch.setattr(vl.sys, "argv",
                        ["verify_ledger.py", "--market", "es", "--verify-ots"])
    assert vl.main() == 0
    assert needle in capsys.readouterr().out


def test_verify_ots_survives_subprocess_error(market, monkeypatch, capsys):
    _faithful(market); _anchored(market)
    def boom(*a, **k):
        raise OSError("ots client missing")
    monkeypatch.setattr(vl.subprocess, "run", boom)
    monkeypatch.setattr(vl.sys, "argv",
                        ["verify_ledger.py", "--market", "es", "--verify-ots"])
    assert vl.main() == 0
    assert "could not run" in capsys.readouterr().out


# ---- remaining detector branches (gated -> must be tested) -------------------

def test_helpers_return_empty_on_missing_files(tmp_path):
    miss = tmp_path / "nope"
    assert vl.load_prices(miss) == {}
    assert vl.load_jsonl(miss) == []
    assert list(vl.prefix_hashes(miss).values()) == [0]


def test_empty_market_notes_no_receipts(market):
    _write(market, [], [], prices=[])
    rep = vl.verify_market("es", verify_ots=False)
    assert any("no receipts yet" in m for m in rep.info)


def test_duplicate_receipt_is_caught(market):
    rec, _ = _faithful(market)
    (market / "receipts.jsonl").write_text(
        json.dumps(rec) + "\n" + json.dumps(rec) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("duplicate receipt" in f for f in rep.fails), rep.fails


def test_double_settlement_is_caught(market):
    _, entry = _faithful(market)
    (market / "ledger.jsonl").write_text(
        json.dumps(entry) + "\n" + json.dumps(entry) + "\n")
    rep = vl.verify_market("es", verify_ots=False)
    assert any("double settlement" in f for f in rep.fails), rep.fails


def test_settled_against_missing_hours_is_caught(market):
    _faithful(market)
    prices = [p for p in json.loads((market / "prices.json").read_text())
              if p["ts"] != f"{TARGET}T05:00:00"]      # a settled buy hour is gone
    (market / "prices.json").write_text(json.dumps(prices))
    rep = vl.verify_market("es", verify_ots=False)
    assert any("hours missing from prices" in f for f in rep.fails), rep.fails


def test_capture_none_vs_number_is_caught(market):
    # flat prices -> oracle P&L <= 0 -> re-derived capture is None; a ledger that
    # records a number instead is a mismatch.
    flat = [{"ts": f"{TARGET}T{h:02d}:00:00", "price": 10.0} for h in range(24)]
    actual = {h: 10.0 for h in range(24)}
    buy, sell = [0, 1], [22, 23]
    p = vl.pnl(buy, sell, actual, PARAMS)
    ob, os_ = vl.pick_extremes(actual, 2)
    orc = vl.pnl(ob, os_, actual, PARAMS)
    rec = {"target": TARGET, "basis_day": BASIS, "buy_hours": buy, "sell_hours": sell,
           "strategy": "battery-2h2h-persistence", "strategy_version": "1",
           "params": PARAMS, "committed_at": f"{BASIS}T09:00:00+00:00"}
    entry = {"target": TARGET, "strategy": rec["strategy"], "strategy_version": "1",
             "buy_hours": buy, "sell_hours": sell,
             "buy_prices": [10.0, 10.0], "sell_prices": [10.0, 10.0],
             "pnl_eur": p, "oracle_pnl_eur": orc, "capture": 0.5}   # should be None
    _write(market, [rec], [entry], prices=flat)
    rep = vl.verify_market("es", verify_ots=False)
    assert any("re-derived None" in f for f in rep.fails), rep.fails
