"""The feasibility measurement must itself be right before its numbers go into
a public disclosure (red-team finding 2026-10-01: the ledger's P&L pairs a day's
sells with its buys regardless of order). Hand-checked cases for each reading."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "feasibility_report", ROOT / "scripts" / "feasibility_report.py")
fr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fr)

P = {"power_mw": 1.0, "rt_eff": 0.85, "fee_eur_mwh": 0.5}
# cheap midday (hours 12, 13 = 10), dear morning (hours 6, 7 = 100) and evening (20, 21 = 90)
DAY = {h: 50.0 for h in range(24)} | {12: 10.0, 13: 10.0, 6: 100.0, 7: 100.0, 20: 90.0, 21: 90.0}
BOOKED = lambda buy, sell: 0.85 * sum(DAY[h] for h in sell) - sum(DAY[h] for h in buy) - 0.5 * (2 + 1.7)


def test_out_of_order_is_about_stored_energy_not_just_first_hour():
    assert not fr.out_of_order([12, 13], [20, 21])        # buy buy sell sell
    assert not fr.out_of_order([5, 13], [7, 21])          # buy sell buy sell
    assert fr.out_of_order([12, 13], [6, 7])              # both sells first
    assert fr.out_of_order([5, 13], [6, 7])               # second sell has nothing left
    assert fr.out_of_order([12, 13], [6, 20])             # first sell has nothing


def test_in_order_day_dispatches_exactly_the_booked_pnl():
    pnl, soc = fr.dispatch([12, 13], [20, 21], DAY, P)
    assert pnl == pytest.approx(BOOKED([12, 13], [20, 21])) and soc == 0


def test_sell_first_day_from_empty_only_buys():
    """The ledger books +148.15 for this day; a battery that starts empty sells
    nothing in the morning, then buys at midday and is left holding the energy."""
    assert BOOKED([12, 13], [6, 7]) == pytest.approx(148.15)
    pnl, soc = fr.dispatch([12, 13], [6, 7], DAY, P)
    assert pnl == pytest.approx(-(10 + 10) - 0.5 * 2) and soc == 2


def test_sell_first_day_runs_if_the_battery_arrives_charged():
    pnl, soc = fr.dispatch([12, 13], [6, 7], DAY, P, soc=2.0)
    assert pnl == pytest.approx(BOOKED([12, 13], [6, 7])) and soc == 2    # refilled at midday


def test_a_full_battery_cannot_buy_until_it_has_sold():
    pnl, soc = fr.dispatch([12, 13], [20, 21], DAY, P, soc=2.0)          # no room at midday
    assert pnl == pytest.approx(0.85 * 180 - 0.5 * 1.7) and soc == 0


def test_feasible_oracle_is_the_best_in_order_schedule():
    """Unconstrained, the two dearest hours are 6 and 7 (before the cheap midday).
    In order, the best is: buy 12, 13 at 10 and sell 20, 21 at 90."""
    assert fr.feasible_oracle(DAY, 2, P) == pytest.approx(BOOKED([12, 13], [20, 21]))
    assert BOOKED([12, 13], [6, 7]) > fr.feasible_oracle(DAY, 2, P)


def _market(tmp_path, monkeypatch, schedules):
    monkeypatch.setattr(fr, "DATA", tmp_path)
    d = tmp_path / "xx"; d.mkdir()
    prices, receipts, ledger = {}, [], []
    for i, (buy, sell) in enumerate(schedules, start=1):
        day = f"2026-01-{i:02d}"
        prices.update({f"{day}T{h:02d}": p for h, p in DAY.items()})
        key = {"target": day, "strategy": "s", "strategy_version": "1"}
        receipts.append(key | {"buy_hours": buy, "sell_hours": sell, "params": P})
        ledger.append(key | {"buy_hours": buy, "sell_hours": sell,
                             "pnl_eur": round(BOOKED(buy, sell), 2), "oracle_pnl_eur": 148.15})
    (d / "prices.json").write_text(json.dumps(prices))          # the dict form
    (d / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in receipts))
    (d / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ledger))
    return d


def test_market_report_separates_the_two_readings(tmp_path, monkeypatch):
    """Two sell-first days in a row. Daily reset: neither morning sale happens.
    Continuous: day 1's midday purchase is what day 2 sells in the morning."""
    _market(tmp_path, monkeypatch, [([12, 13], [6, 7]), ([12, 13], [6, 7])])
    t = fr.market_report("xx")["s"]
    assert (t["days"], t["out_of_order"]) == (2, 2)
    assert t["booked"] == pytest.approx(2 * 148.15)
    assert t["daily_reset"] == pytest.approx(2 * -21.0)
    assert t["continuous"] == pytest.approx(-21.0 + 148.15)
    assert t["oracle_feasible"] == pytest.approx(2 * BOOKED([12, 13], [20, 21]))


def test_market_report_reads_list_prices_and_main_prints_a_table(tmp_path, monkeypatch, capsys):
    d = _market(tmp_path, monkeypatch, [([12, 13], [20, 21])])
    flat = json.loads((d / "prices.json").read_text())
    (d / "prices.json").write_text(json.dumps([{"ts": k, "price": v} for k, v in flat.items()]))
    assert fr.main(["--market", "xx"]) == 0 and fr.main(["--all"]) == 0
    out = capsys.readouterr().out
    assert "XX" in out and "out of order" in out and "(100%)" in out
