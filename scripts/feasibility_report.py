#!/usr/bin/env python3
"""How much of the booked P&L could a battery actually have dispatched?

The ledger's P&L formula pairs each day's two sell hours with the same day's two
buy hours and ignores their ORDER (VERIFY.md section 4; red-team finding of
2026-10-01). On a day where a sell hour comes before the buy hours, a battery
that starts the day empty cannot run the schedule. This script measures that,
from the public audit files alone, under two readings:

  continuous   one battery follows every receipt in time order and carries its
               charge across midnight (empty before the first receipt). The
               closer of the two to a real asset.
  daily-reset  the battery is empty at every midnight. The strict reading:
               energy still stored at the end of a day earns nothing.

In both: 1 MW per chosen hour, capacity = the day's number of buy hours, a sale
needs energy already stored, a buy needs room, and the receipt's own efficiency
and fee apply to whatever actually moves.

It is a MEASUREMENT. It changes no ledger row and no decision rule: the
pre-registered comparisons are defined on the booked P&L.

    python3 scripts/feasibility_report.py            # ES
    python3 scripts/feasibility_report.py --all      # every market under Data/
    python3 scripts/feasibility_report.py --confirmatory-executable --as-of ISO

--confirmatory-executable is the block the reporting note for Spain's
confirmatory look requires (docs/multiple-comparisons-policy.md, "Reporting
note for the confirmatory look"): over exactly the 90 non-tied days that decide
climatology vs persistence, each day's daily-reset executable P&L for both,
paired into wins, ties and losses, and both strategies' booked and executable
totals over those days. NOT PRE-REGISTERED: it decides nothing. It prints
nothing but one line until the confirmatory look is reached at --as-of, and
--as-of is required, so the figures belong to one instant. It FAILS CLOSED:
if the ledger cannot be read or parsed, or any counting row of the pair
(version 1, a P&L, settled no later than --as-of, target from 2026-08-01) has
a null or non-finite pnl_eur, it prints one fixed line with no ledger detail
and exits 1, so it never prints where compare_strategies would fail or count
differently.

LIMIT: like compare_strategies --decision-look, it prints whenever it is run
once the 90th non-tied day has settled at --as-of. The release point is the
month-end review, a procedure; the rules define no cutoff instant for code to
check.

NOBODY RUNS IT ON THE REAL LEDGER BEFORE THE MONTH-END REVIEW THAT TAKES THE
CONFIRMATORY LOOK. The note says these figures are computed once, at the look,
at the look's own instant, and the statistical referee recomputes them with the
rest. Its tests use synthetic ledgers only.

The day set is selected HERE, independently of scripts/compare_strategies.py
(which imports talea), under the same rules: version-1 rows of the two
strategies with a P&L, settled no later than --as-of, targets from 2026-08-01;
a day counts when both have a row; the daily delta rounded to cents is a tie
below 0.01 EUR; the set stops at the 90th non-tied day. The tests check that
both scripts pick the same window.

Stdlib-only and imports nothing from talea, like verify_ledger.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "Data"


def load_prices(path: Path) -> dict[str, float]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    return {r["ts"]: float(r["price"]) for r in raw}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def out_of_order(buy: list[int], sell: list[int]) -> bool:
    """True if, starting empty, some sell hour arrives with nothing stored."""
    soc = 0
    for _hour, kind in sorted([(h, "b") for h in buy] + [(h, "s") for h in sell]):
        if kind == "b":
            soc += 1
        elif soc < 1:
            return True
        else:
            soc -= 1
    return False


def dispatch(buy: list[int], sell: list[int], actual: dict[int, float],
             params: dict, soc: float = 0.0) -> tuple[float, float]:
    """Run one day's schedule in hour order from `soc` MWh stored.
    Returns (pnl, soc_at_end). A buy with no room and a sell with nothing
    stored simply do not happen."""
    power, eff, fee = params["power_mw"], params["rt_eff"], params["fee_eur_mwh"]
    cap = power * len(buy)
    cost = revenue = moved = 0.0
    for hour, kind in sorted([(h, "b") for h in buy] + [(h, "s") for h in sell]):
        if kind == "b" and soc + power <= cap + 1e-9:
            soc += power
            cost += actual[hour] * power
            moved += power
        elif kind == "s" and soc >= power - 1e-9:
            soc -= power
            revenue += actual[hour] * power * eff
            moved += power * eff
    return revenue - cost - fee * moved, soc


def feasible_oracle(actual: dict[int, float], n: int, params: dict) -> float:
    """Best P&L any in-order schedule of n buys and n sells could have made
    that day, starting and ending empty. Brute force; n is 2."""
    hours = sorted(actual)
    best = float("-inf")
    for buy in combinations(hours, n):
        for sell in combinations(hours, n):
            if set(buy) & set(sell) or out_of_order(list(buy), list(sell)):
                continue
            best = max(best, dispatch(list(buy), list(sell), actual, params)[0])
    return best


def market_report(slug: str) -> dict[str, dict]:
    """{strategy: totals} for one market, over its settled rows."""
    d = DATA / slug
    prices = load_prices(d / "prices.json")
    params_by_key = {(r["target"], r["strategy"], r["strategy_version"]): r.get("params", {})
                     for r in load_jsonl(d / "receipts.jsonl")}
    by_day: dict[str, dict[int, float]] = defaultdict(dict)
    for ts, p in prices.items():
        by_day[ts[:10]][int(ts[11:13])] = p
    out: dict[str, dict] = {}
    rows = sorted(load_jsonl(d / "ledger.jsonl"), key=lambda e: e["target"])
    oracle_cache: dict[str, float] = {}
    carried: dict[str, float] = defaultdict(float)     # continuous battery, per strategy
    for e in rows:
        key = (e["target"], e["strategy"], e["strategy_version"])
        params = params_by_key[key]
        actual = by_day[e["target"]]
        t = out.setdefault(e["strategy"], {
            "days": 0, "out_of_order": 0, "booked": 0.0, "continuous": 0.0,
            "daily_reset": 0.0, "oracle_booked": 0.0, "oracle_feasible": 0.0})
        t["days"] += 1
        t["out_of_order"] += out_of_order(e["buy_hours"], e["sell_hours"])
        t["booked"] += e["pnl_eur"]
        t["daily_reset"] += dispatch(e["buy_hours"], e["sell_hours"], actual, params)[0]
        pnl, carried[e["strategy"]] = dispatch(
            e["buy_hours"], e["sell_hours"], actual, params, carried[e["strategy"]])
        t["continuous"] += pnl
        t["oracle_booked"] += e["oracle_pnl_eur"]
        if e["target"] not in oracle_cache:
            oracle_cache[e["target"]] = feasible_oracle(actual, len(e["buy_hours"]), params)
        t["oracle_feasible"] += oracle_cache[e["target"]]
    return out


# ---- the confirmatory pair's executable figures (not pre-registered) ----------

CONF_MARKET = "es"
CONF_A = "battery-2h2h-climatology"
CONF_B = "battery-2h2h-persistence"
CONF_SINCE = "2026-08-01"
CONF_N = 90
TIE_EPS = 0.01
REFUSED = ("ES confirmatory pair: REFUSED. The ledger could not be read, or a counting "
           "row of the pair is not as the rules require. No figure is printed.")


class Refused(Exception):
    """The pair's rows cannot be counted as compare_strategies counts them."""


def _instant(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _row_counts(row: dict, as_of: datetime) -> bool:
    """compare_strategies' rule for the confirmatory look: a row with a P&L,
    not settled later than as_of (a row with no settlement time counts)."""
    if "pnl_eur" not in row:
        return False
    ts = row.get("settled_at")
    return not (ts and _instant(ts) > as_of)


def _finite_pnl(row: dict) -> float:
    """Accept exactly what compare_strategies counts as a number (a bool too,
    at its numeric value, as Python arithmetic gives it there); refuse null,
    NaN and +-inf."""
    v = row["pnl_eur"]
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Refused("pnl_eur")
    return float(v)


def confirmatory_days(rows: list[dict], as_of: datetime) -> dict:
    """The confirmatory comparison's day set at as_of: {"reached", "days"
    (the first CONF_N non-tied days, in order), "selected" (every shared day up
    to the CONF_N-th non-tied one, in order, as (day, is_tie)), "shared",
    "ties"}. Raises Refused if any counting row's pnl_eur is null or not
    finite, wherever it lies."""
    by_day: dict[str, dict[str, dict]] = {}
    for r in rows:
        if str(r.get("strategy_version") or "1") != "1" or r["strategy"] not in (CONF_A, CONF_B):
            continue
        if _row_counts(r, as_of) and r["target"] >= CONF_SINCE:
            _finite_pnl(r)
            by_day.setdefault(r["target"], {})[r["strategy"]] = r
    days: list[str] = []
    selected: list[tuple[str, bool]] = []
    for day in sorted(by_day):
        v = by_day[day]
        if CONF_A not in v or CONF_B not in v:
            continue
        tie = abs(round(v[CONF_A]["pnl_eur"] - v[CONF_B]["pnl_eur"], 2)) < TIE_EPS
        selected.append((day, tie))
        if not tie:
            days.append(day)
            if len(days) == CONF_N:
                break
    reached = len(days) == CONF_N
    if not reached:
        return {"reached": False, "days": [], "selected": [], "shared": None,
                "ties": None, "rows": {}}
    return {"reached": True, "days": days, "selected": selected, "shared": len(selected),
            "ties": sum(t for _, t in selected), "rows": by_day}


def _wtl(deltas: list[float]) -> tuple[int, int, int]:
    d = [round(x, 2) for x in deltas]
    return (sum(x >= TIE_EPS for x in d), sum(abs(x) < TIE_EPS for x in d),
            sum(x <= -TIE_EPS for x in d))


def confirmatory_executable(as_of: datetime, slug: str = CONF_MARKET) -> dict:
    """The reporting note's block, or {"reached": False} before the look.
    Raises Refused, carrying no ledger detail, if the ledger cannot be read,
    parsed or counted as the rules require."""
    d = DATA / slug
    try:
        sel = confirmatory_days(load_jsonl(d / "ledger.jsonl"), as_of)
    except Exception:                 # noqa: BLE001 — any detail could name a row
        raise Refused("ledger") from None
    if not sel["reached"]:
        return {"reached": False}
    prices = load_prices(d / "prices.json")
    params = {(r["target"], r["strategy"], str(r.get("strategy_version") or "1")): r.get("params", {})
              for r in load_jsonl(d / "receipts.jsonl")}
    hourly: dict[str, dict[int, float]] = defaultdict(dict)
    for ts, p in prices.items():
        hourly[ts[:10]][int(ts[11:13])] = p
    booked = {CONF_A: 0.0, CONF_B: 0.0}
    executable = {CONF_A: 0.0, CONF_B: 0.0}
    deltas = []
    for day in sel["days"]:
        x = {}
        for s in (CONF_A, CONF_B):
            e = sel["rows"][day][s]
            booked[s] += e["pnl_eur"]
            x[s] = dispatch(e["buy_hours"], e["sell_hours"], hourly[day],
                            params[(day, s, "1")])[0]
            executable[s] += x[s]
        deltas.append(x[CONF_A] - x[CONF_B])
    wins, ties, losses = _wtl(deltas)
    return {"reached": True, "as_of": as_of.isoformat(), "first_day": sel["days"][0],
            "last_day": sel["days"][-1], "shared": sel["shared"], "booked_ties": sel["ties"],
            "wins": wins, "ties": ties, "losses": losses,
            "booked": booked, "executable": executable}


def format_confirmatory_executable(r: dict) -> str:
    if not r["reached"]:
        return ("ES confirmatory pair: the confirmatory look is not reached at this "
                "instant; no executable figures (they are computed once, at the look).")
    a, b = CONF_A, CONF_B
    return "\n".join([
        "ES confirmatory pair, executable figures — NOT PRE-REGISTERED, DECIDES NOTHING",
        "  valid only at the month-end review that takes the confirmatory look "
        "(Amendment 1, clause 5)",
        f"  as of {r['as_of']}; the verdict's {CONF_N} non-tied days "
        f"{r['first_day']}..{r['last_day']} ({r['shared']} shared days; their "
        f"{r['booked_ties']} booked ties are not among them)",
        f"  daily-reset executable, climatology better / tied / worse: "
        f"{r['wins']} / {r['ties']} / {r['losses']}",
        f"  booked totals:     climatology {r['booked'][a]:,.2f}  persistence {r['booked'][b]:,.2f}",
        f"  executable totals: climatology {r['executable'][a]:,.2f}  "
        f"persistence {r['executable'][b]:,.2f}",
        "  No claim is made from these figures."])


def format_market(slug: str, report: dict[str, dict]) -> str:
    lines = [f"{slug.upper()}",
             f"  {'strategy':<26}{'days':>5}{'out of order':>14}{'booked':>12}"
             f"{'continuous':>19}{'daily-reset':>19}{'feasible oracle':>17}"]
    for name in sorted(report):
        t = report[name]
        share = lambda x: f"{x:>11,.2f} ({x / t['booked']:>4.0%})" if t["booked"] else f"{x:>11,.2f}"
        lines.append(
            f"  {name:<26}{t['days']:>5}{t['out_of_order']:>7} ({t['out_of_order'] / t['days']:>4.0%})"
            f"{t['booked']:>12,.2f}{share(t['continuous'])}{share(t['daily_reset'])}"
            f"{t['oracle_feasible']:>17,.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", default="es")
    ap.add_argument("--all", action="store_true", help="every market under Data/")
    ap.add_argument("--confirmatory-executable", action="store_true",
                    help="the reporting note's not-pre-registered block for Spain's "
                         "confirmatory look; only at the month-end review that takes it")
    ap.add_argument("--as-of", help="ISO instant; required with --confirmatory-executable")
    args = ap.parse_args(argv)
    if args.confirmatory_executable:
        if not args.as_of or args.all or args.market != CONF_MARKET:
            ap.error("--confirmatory-executable needs --as-of and takes neither --all "
                     "nor another --market")
        try:
            as_of = _instant(args.as_of)
        except ValueError:
            ap.error("--as-of is not an ISO instant")
        try:
            result = confirmatory_executable(as_of)
        except Refused:
            print(REFUSED)
            return 1
        print(format_confirmatory_executable(result))
        return 0
    if args.as_of:
        ap.error("--as-of is used only with --confirmatory-executable")
    slugs = (sorted(p.parent.name for p in DATA.glob("*/ledger.jsonl"))
             if args.all else [args.market])
    for slug in slugs:
        print(format_market(slug, market_report(slug)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
