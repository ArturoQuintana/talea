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

Stdlib-only and imports nothing from talea, like verify_ledger.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
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
    args = ap.parse_args(argv)
    slugs = (sorted(p.parent.name for p in DATA.glob("*/ledger.jsonl"))
             if args.all else [args.market])
    for slug in slugs:
        print(format_market(slug, market_report(slug)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
