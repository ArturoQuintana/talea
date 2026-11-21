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


# ---- --confirmatory-executable: SYNTHETIC ledgers only ------------------------
# The reporting note for Spain's confirmatory look says its executable figures
# are computed once, at the look. These tests never read the real Data/.

from datetime import date, datetime, timedelta, timezone  # noqa: E402

_CS_SPEC = importlib.util.spec_from_file_location(
    "compare_strategies", ROOT / "scripts" / "compare_strategies.py")
cs = importlib.util.module_from_spec(_CS_SPEC)
_CS_SPEC.loader.exec_module(cs)

IN_ORDER, SELL_FIRST = ([12, 13], [20, 21]), ([12, 13], [6, 7])   # booked 131.15, 148.15
CLIM, PERS = fr.CONF_A, fr.CONF_B


def _conf_ledger(tmp_path, monkeypatch, n_days, *, start=date(2026, 7, 29), extra=()):
    """Day k (from `start`): every 10th is a tie (both in order). The other days
    alternate, counting from 2026-08-01: climatology in order vs persistence
    sell-first (booked: climatology LOSES, executable: it WINS), then the
    reverse. Each row is settled at noon UTC the day after its target."""
    monkeypatch.setattr(fr, "DATA", tmp_path)
    d = tmp_path / "es"; d.mkdir()
    prices, receipts, ledger = {}, [], []
    j = 0                                   # non-tied days since 2026-08-01
    for k in range(n_days):
        day = (start + timedelta(days=k)).isoformat()
        prices.update({f"{day}T{h:02d}": p for h, p in DAY.items()})
        if k % 10 == 0:
            plan = {CLIM: IN_ORDER, PERS: IN_ORDER}
        else:
            plan = ({CLIM: IN_ORDER, PERS: SELL_FIRST} if j % 2 == 0
                    else {CLIM: SELL_FIRST, PERS: IN_ORDER})
            j += day >= "2026-08-01"
        settled = f"{(start + timedelta(days=k + 1)).isoformat()}T12:00:00+00:00"
        for s, (buy, sell) in plan.items():
            key = {"target": day, "strategy": s, "strategy_version": "1"}
            receipts.append(key | {"buy_hours": buy, "sell_hours": sell, "params": P})
            ledger.append(key | {"buy_hours": buy, "sell_hours": sell, "settled_at": settled,
                                 "pnl_eur": round(BOOKED(buy, sell), 2), "oracle_pnl_eur": 148.15})
    ledger.extend(extra)
    (d / "prices.json").write_text(json.dumps(prices))
    (d / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in receipts))
    (d / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ledger))
    return d


AS_OF_LATE = datetime(2027, 6, 1, tzinfo=timezone.utc)


def test_before_the_look_it_prints_one_line_and_no_figure(tmp_path, monkeypatch, capsys):
    _conf_ledger(tmp_path, monkeypatch, 60)           # well short of 90 non-tied days
    assert fr.confirmatory_executable(AS_OF_LATE) == {"reached": False}
    assert fr.main(["--confirmatory-executable", "--as-of", AS_OF_LATE.isoformat()]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "not reached" in out[0] and not any(c.isdigit() for c in out[0])


def test_as_of_hides_rows_settled_later(tmp_path, monkeypatch):
    _conf_ledger(tmp_path, monkeypatch, 140)
    # 2026-07-29..31 are before 2026-08-01; by this instant only 61 days have settled.
    assert not fr.confirmatory_executable(datetime(2026, 10, 1, tzinfo=timezone.utc))["reached"]
    assert fr.confirmatory_executable(AS_OF_LATE)["reached"]


def _compare_selection(rows, as_of):
    """compare_strategies' own day selection, reconstructed day by day from its
    public pair(): every shared day since 2026-08-01, in order, as (day, is_tie),
    up to its 90th non-tied day. Each day is classified by pair() itself, so its
    version filter, as-of rule and tie rule all apply."""
    out, n = [], 0
    for day in sorted({r["target"] for r in rows}):
        one = cs.pair([r for r in rows if r["target"] == day], CLIM, PERS,
                      as_of=as_of, since_target=cs.CONFIRMATORY["since"])
        if one["shared"]:
            out.append((day, bool(one["ties"])))
            n += not one["ties"]
            if n == cs.CONFIRMATORY["n"]:
                break
    return out


def _v2(day, pnl):
    return {"target": day, "strategy": CLIM, "strategy_version": "2",
            "settled_at": "2026-12-01T12:00:00+00:00", "pnl_eur": pnl, "oracle_pnl_eur": 148.15}


def test_the_window_is_compare_strategies_window(tmp_path, monkeypatch):
    """The two scripts select the days independently; they must select the same
    ordered list, ties marked the same. The v2 rows are written AFTER the v1
    rows, so a selection that dropped the version filter would let them replace
    v1: 2026-08-08 (a tie in v1) would turn non-tied, and 2026-08-02 (non-tied
    in v1: climatology 148.15 vs persistence 131.15) would turn into a tie."""
    extra = [_v2("2026-08-08", 999.0), _v2("2026-08-02", 131.15)]
    d = _conf_ledger(tmp_path, monkeypatch, 140, extra=extra)
    rows = [json.loads(ln) for ln in (d / "ledger.jsonl").read_text().splitlines()]
    mine = fr.confirmatory_days(rows, AS_OF_LATE)
    theirs = _compare_selection(rows, AS_OF_LATE)
    assert mine["reached"] and mine["selected"] == theirs
    assert ("2026-08-08", True) in theirs and ("2026-08-02", False) in theirs
    look = cs.confirmatory_look(rows, None, as_of=AS_OF_LATE)       # the real function agrees
    assert (look["first_day"], look["last_day"], look["shared"], look["ties"]) == \
        (theirs[0][0], theirs[-1][0], len(theirs), sum(t for _, t in theirs))
    assert theirs[0][0] == "2026-08-01"                            # 07-29..07-31 excluded


def test_executable_pairing_is_hand_checked(tmp_path, monkeypatch):
    """Of the 90 non-tied days, 45 are 'climatology in order, persistence
    sell-first' and 45 the reverse. Booked, each kind is +-17.00 (148.15 vs
    131.15); executable, the in-order side keeps 131.15 and the sell-first
    side, from empty, only buys: -21.00."""
    _conf_ledger(tmp_path, monkeypatch, 140)
    r = fr.confirmatory_executable(AS_OF_LATE)
    assert (r["wins"], r["ties"], r["losses"]) == (45, 0, 45)
    assert r["booked"][CLIM] == pytest.approx(45 * 131.15 + 45 * 148.15)
    assert r["booked"][PERS] == pytest.approx(r["booked"][CLIM])
    assert r["executable"][CLIM] == pytest.approx(45 * 131.15 - 45 * 21.0)
    assert r["executable"][PERS] == pytest.approx(r["executable"][CLIM])


def test_an_executable_tie_is_counted_as_a_tie(tmp_path, monkeypatch):
    """Two sell-first schedules a cent apart in booked P&L are not a booked tie
    if their booked P&L differ, but both only buy from empty: executable tie."""
    _conf_ledger(tmp_path, monkeypatch, 140)
    led = tmp_path / "es" / "ledger.jsonl"
    rows = [json.loads(ln) for ln in led.read_text().splitlines()]
    for e in rows:                                 # make 2026-08-02 both sell-first, booked apart
        if e["target"] == "2026-08-02":
            e["buy_hours"], e["sell_hours"] = SELL_FIRST
            e["pnl_eur"] = 148.15 if e["strategy"] == CLIM else 140.00
    led.write_text("".join(json.dumps(e) + "\n" for e in rows))
    r = fr.confirmatory_executable(AS_OF_LATE)
    assert r["ties"] == 1 and r["wins"] + r["losses"] == 89


def test_the_cli_requires_as_of_and_refuses_other_markets(tmp_path, monkeypatch, capsys):
    _conf_ledger(tmp_path, monkeypatch, 140)
    for argv in (["--confirmatory-executable"],
                 ["--confirmatory-executable", "--all", "--as-of", "2027-01-01"],
                 ["--confirmatory-executable", "--market", "de", "--as-of", "2027-01-01"],
                 ["--as-of", "2027-01-01"]):
        with pytest.raises(SystemExit) as e:
            fr.main(argv)
        assert e.value.code == 2
    assert fr.main(["--confirmatory-executable", "--as-of", "2027-06-01T00:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "NOT PRE-REGISTERED, DECIDES NOTHING" in out and "45 / 0 / 45" in out
    assert ("valid only at the month-end review that takes the confirmatory look "
            "(Amendment 1, clause 5)") in out
    assert "No claim is made from these figures." in out


def test_the_docstring_forbids_an_early_run_on_the_real_ledger():
    doc = " ".join(fr.__doc__.split())
    assert "NOBODY RUNS IT ON THE REAL LEDGER BEFORE THE MONTH-END REVIEW" in doc
    assert "NOT PRE-REGISTERED: it decides nothing" in doc


def test_a_bool_pnl_counts_as_compare_counts_it(tmp_path, monkeypatch, capsys):
    """compare_strategies' pair() does arithmetic on pnl_eur, so true - 0.0 is
    a 1.00 win and false - 0.0 a tie. Parity is the property: the selection must
    be the same, and the run must not refuse."""
    d = _conf_ledger(tmp_path, monkeypatch, 140)
    flips = {"2026-08-03": True, "2026-08-04": False}       # both non-tied in the fixture
    def change(ls):
        out = []
        for ln in ls:
            e = json.loads(ln)
            if e["target"] in flips:
                e["pnl_eur"] = flips[e["target"]] if e["strategy"] == CLIM else 0.0
            out.append(json.dumps(e))
        return out
    _rewrite(tmp_path, change)
    rows = [json.loads(ln) for ln in (d / "ledger.jsonl").read_text().splitlines()]
    theirs = _compare_selection(rows, AS_OF_LATE)
    assert ("2026-08-03", False) in theirs and ("2026-08-04", True) in theirs
    assert fr.confirmatory_days(rows, AS_OF_LATE)["selected"] == theirs
    assert fr.main(["--confirmatory-executable", "--as-of", AS_OF_LATE.isoformat()]) == 0
    assert fr.REFUSED not in capsys.readouterr().out


def _refused(capsys):
    assert fr.main(["--confirmatory-executable", "--as-of", AS_OF_LATE.isoformat()]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [fr.REFUSED] and captured.err == ""


def _rewrite(tmp_path, change):
    led = tmp_path / "es" / "ledger.jsonl"
    lines = led.read_text().splitlines()
    led.write_text("\n".join(change(lines)) + "\n")


def test_invalid_json_is_refused_with_the_fixed_line(tmp_path, monkeypatch, capsys):
    _conf_ledger(tmp_path, monkeypatch, 140)
    _rewrite(tmp_path, lambda ls: ls[:50] + ['{"target": "2026-09-1'] + ls[50:])
    _refused(capsys)


def test_a_malformed_settled_at_is_refused_with_the_fixed_line(tmp_path, monkeypatch, capsys):
    _conf_ledger(tmp_path, monkeypatch, 140)
    def change(ls):
        e = json.loads(ls[60]); e["settled_at"] = "yesterday, 13:00"
        return ls[:60] + [json.dumps(e)] + ls[61:]
    _rewrite(tmp_path, change)
    _refused(capsys)


@pytest.mark.parametrize("bad", [None, "NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("where", ["inside", "outside"])
def test_a_null_or_non_finite_pnl_is_refused_anywhere(tmp_path, monkeypatch, capsys, bad, where):
    """Inside the 90-day window, or after it (2026-12-10, still settled before
    --as-of): either way the run refuses."""
    _conf_ledger(tmp_path, monkeypatch, 140)
    day = "2026-08-20" if where == "inside" else "2026-12-10"
    def change(ls):
        out = []
        for ln in ls:
            e = json.loads(ln)
            if e["target"] == day and e["strategy"] == PERS:
                ln = ln.replace(f'"pnl_eur": {e["pnl_eur"]}', f'"pnl_eur": {json.dumps(bad) if bad is None else bad}')
                assert ln != json.dumps(e)
            out.append(ln)
        return out
    _rewrite(tmp_path, change)
    _refused(capsys)


def test_a_bad_pnl_that_does_not_count_is_ignored(tmp_path, monkeypatch, capsys):
    """Before 2026-08-01, or in a v2 row, a null P&L is not a counting row."""
    _conf_ledger(tmp_path, monkeypatch, 140,
                 extra=[_v2("2026-08-05", None) | {"pnl_eur": None},
                        {"target": "2026-07-30", "strategy": PERS, "strategy_version": "1",
                         "settled_at": "2026-07-31T12:00:00+00:00", "pnl_eur": None}])
    assert fr.main(["--confirmatory-executable", "--as-of", AS_OF_LATE.isoformat()]) == 0
    assert "45 / 0 / 45" in capsys.readouterr().out
