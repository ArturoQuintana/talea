#!/usr/bin/env python3
"""Independent re-derivation of the esios-paper ledger — "don't trust us, re-run it".

Recomputes every settlement from the dataset of record (prices.json) and the
committed decisions (receipts.jsonl) FROM SCRATCH, using each receipt's OWN
recorded params, and diffs the result against ledger.jsonl. Also checks the
append-only + leak-guard invariants and reports OpenTimestamps coverage.

Deliberately stdlib-only and imports NOTHING from talea: a bug in the
loop's own P&L code cannot hide here, and an auditor can run this file alone.

    uv run python scripts/verify_ledger.py            # ES (the public ledger)
    uv run python scripts/verify_ledger.py --all      # every market
    uv run python scripts/verify_ledger.py --market de
    uv run python scripts/verify_ledger.py --all --verify-ots   # also shell `ots verify`

Exit code 0 = every check passed; 1 = at least one FAIL. WARN/INFO never fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "Data"
PNL_TOL = 0.005      # ledger pnl/oracle are rounded to 2 dp
CAP_TOL = 0.0005     # capture rounded to 3 dp
MIN_DAY_HOURS = 23   # a fully-published day (23 on the DST-spring day)
OTS_CLIENT = "opentimestamps-client==0.7.2"   # pinned (see __main__.OTS_CLIENT)


# --- the P&L math, reimplemented independently (mirrors the doc, not the code) ---

def pick_extremes(actual: dict[int, float], n: int) -> tuple[list[int], list[int]]:
    order = sorted(actual, key=lambda h: (actual[h], h))
    return sorted(order[:n]), sorted(order[-n:])


def pnl(buy: list[int], sell: list[int], actual: dict[int, float], p: dict) -> float:
    power, eff, fee = p["power_mw"], p["rt_eff"], p["fee_eur_mwh"]
    cost = sum(actual[h] * power for h in buy)
    revenue = sum(actual[h] * power * eff for h in sell)
    moved = power * len(buy) + power * eff * len(sell)
    return round(revenue - cost - fee * moved, 2)


# --- IO ---

def load_prices(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    return {r["ts"]: float(r["price"]) for r in raw}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def day_profile(prices: dict[str, float], target: str) -> dict[int, float]:
    return {int(ts[11:13]): p for ts, p in prices.items() if ts[:10] == target}


def prefix_hashes(path: Path) -> dict[str, int]:
    """sha256(first k lines) -> k, for every k. Append-only means a manifest
    taken when the file had k lines hashes the current file's k-line prefix."""
    out = {hashlib.sha256(b"").hexdigest(): 0}
    if not path.exists():
        return out
    running = hashlib.sha256()
    k = 0
    for raw_line in path.read_bytes().splitlines(keepends=True):
        running.update(raw_line)
        k += 1
        out[running.hexdigest()] = k
    return out


# --- the checks ---

class Report:
    def __init__(self, slug: str):
        self.slug = slug
        self.fails: list[str] = []
        self.warns: list[str] = []
        self.info: list[str] = []
        self.checked = 0

    def fail(self, m): self.fails.append(m)
    def warn(self, m): self.warns.append(m)
    def note(self, m): self.info.append(m)


def verify_market(slug: str, verify_ots: bool) -> Report:
    d = DATA / slug                        # ES is Data/es/ now — no special-case
    r = Report(slug)
    prices = load_prices(d / "prices.json")
    receipts = load_jsonl(d / "receipts.jsonl")
    ledger = load_jsonl(d / "ledger.jsonl")
    if not receipts and not ledger:
        r.note("no receipts yet")
        return r
    # (a ledger with NO receipts behind it is not "empty" — it's all orphans,
    #  and must fall through to the orphan check below.)

    # receipts / ledger indexed by the identity key (target, strategy, version)
    rec_by_key: dict[tuple, dict] = {}
    for rec in receipts:
        key = (rec["target"], rec["strategy"], rec["strategy_version"])
        if key in rec_by_key:
            r.fail(f"duplicate receipt for {key}")
        rec_by_key[key] = rec

    led_by_key: dict[tuple, dict] = {}
    for e in ledger:
        key = (e["target"], e["strategy"], e["strategy_version"])
        if key in led_by_key:
            r.fail(f"double settlement for {key}")
        led_by_key[key] = e

    # 1) every settlement re-derived from raw prices + the receipt's own params
    for key, e in led_by_key.items():
        rec = rec_by_key.get(key)
        if rec is None:
            r.fail(f"ledger entry {key} has NO receipt (orphan settlement)")
            continue
        r.checked += 1
        if e["buy_hours"] != rec["buy_hours"] or e["sell_hours"] != rec["sell_hours"]:
            r.fail(f"{key}: settled hours differ from the committed receipt "
                   f"(receipt {rec['buy_hours']}/{rec['sell_hours']} vs "
                   f"ledger {e['buy_hours']}/{e['sell_hours']})")
            continue
        actual = day_profile(prices, e["target"])
        chosen = e["buy_hours"] + e["sell_hours"]
        if not all(h in actual for h in chosen):
            r.fail(f"{key}: settled against hours missing from prices.json")
            continue
        exp_buy_p = [actual[h] for h in e["buy_hours"]]
        exp_sell_p = [actual[h] for h in e["sell_hours"]]
        if (max((abs(a - b) for a, b in zip(exp_buy_p, e["buy_prices"])), default=0) > 1e-6
                or max((abs(a - b) for a, b in zip(exp_sell_p, e["sell_prices"])),
                       default=0) > 1e-6):
            r.fail(f"{key}: recorded buy/sell prices don't match prices.json")
        params = rec.get("params", {})
        got_pnl = pnl(e["buy_hours"], e["sell_hours"], actual, params)
        if abs(got_pnl - e["pnl_eur"]) > PNL_TOL:
            r.fail(f"{key}: pnl re-derived {got_pnl} != ledger {e['pnl_eur']}")
        ob, os_ = pick_extremes(actual, len(e["buy_hours"]))
        got_oracle = pnl(ob, os_, actual, params)
        if abs(got_oracle - e["oracle_pnl_eur"]) > PNL_TOL:
            r.fail(f"{key}: oracle re-derived {got_oracle} != ledger "
                   f"{e['oracle_pnl_eur']}")
        exp_cap = round(got_pnl / got_oracle, 3) if got_oracle > 0 else None
        if exp_cap is None or e.get("capture") is None:
            if exp_cap != e.get("capture"):
                r.fail(f"{key}: capture {e.get('capture')} != re-derived {exp_cap}")
        elif abs(exp_cap - e["capture"]) > CAP_TOL:
            r.fail(f"{key}: capture {e['capture']} != re-derived {exp_cap}")

    # 2) leak-guard / temporal integrity on every receipt
    for key, rec in rec_by_key.items():
        target = date.fromisoformat(rec["target"])
        basis = date.fromisoformat(rec["basis_day"])
        if basis >= target:
            r.fail(f"{key}: basis_day {basis} not before target {target}")
        committed = datetime.fromisoformat(rec["committed_at"]).date()
        if committed >= target:
            r.fail(f"{key}: committed_at {committed} is ON/AFTER target {target} "
                   f"— LEAK (a receipt must predate its target day)")
        elif committed > basis:
            r.warn(f"{key}: committed_at {committed} later than basis_day {basis} "
                   f"(late but pre-target — recovery tick?)")

    # 3) receipts whose day is fully published but not yet settled
    for key, rec in rec_by_key.items():
        if key in led_by_key:
            continue
        actual = day_profile(prices, rec["target"])
        chosen = rec["buy_hours"] + rec["sell_hours"]
        if len(actual) >= MIN_DAY_HOURS and all(h in actual for h in chosen):
            r.warn(f"{key}: target fully published but UNSETTLED (settles next tick?)")

    # 4) OpenTimestamps coverage (append-only proof + anchor count)
    ots_dir = d / "ots"
    manifests = sorted(ots_dir.glob("*.txt")) if ots_dir.exists() else []
    stamped = [m for m in manifests if m.with_suffix(".txt.ots").exists()]
    rec_prefixes = prefix_hashes(d / "receipts.jsonl")
    led_prefixes = prefix_hashes(d / "ledger.jsonl")
    covered_recs = covered_leds = 0
    for m in stamped:
        rec_h = led_h = None
        for line in m.read_text().splitlines():
            if line.startswith("sha256(receipts.jsonl)="):
                rec_h = line.split("=", 1)[1]
            elif line.startswith("sha256(ledger.jsonl)="):
                led_h = line.split("=", 1)[1]
        if rec_h and rec_h not in ("absent", *rec_prefixes):
            r.fail(f"OTS {m.name}: receipts hash matches NO prefix of the current "
                   f"append-only file — history was rewritten OR proof is foreign")
        elif rec_h in rec_prefixes:
            covered_recs = max(covered_recs, rec_prefixes[rec_h])
        if led_h and led_h not in ("absent", *led_prefixes):
            r.fail(f"OTS {m.name}: ledger hash matches NO prefix of the current file")
        elif led_h in led_prefixes:
            covered_leds = max(covered_leds, led_prefixes[led_h])
    n_rec, n_led = len(receipts), len(ledger)
    # Wording (audit finding 2026-09-07): without --verify-ots this pass only
    # checks that a .ots proof EXISTS next to each manifest and that the manifest
    # hashes a prefix of the current file — it does NOT distinguish a PENDING
    # (calendar-only) proof from a Bitcoin-confirmed one. So the default report
    # says "OTS-stamped"/"OTS-covered", never "anchored"/"Bitcoin-covered";
    # Bitcoin finality is only asserted by the --verify-ots branch below.
    if stamped:
        r.note(f"OTS: {len(stamped)}/{len(manifests)} manifests OTS-stamped; "
               f"receipts OTS-covered {covered_recs}/{n_rec}, "
               f"ledger {covered_leds}/{n_led} "
               f"(uncovered tail = appended since the last stamped manifest; "
               f"Bitcoin confirmation NOT checked here — pass --verify-ots)")
    else:
        r.warn(f"OTS: no OTS-stamped manifests found in {ots_dir}")
    if verify_ots and stamped:
        for m in stamped:
            proof = m.with_suffix(".txt.ots")
            try:
                out = subprocess.run(
                    ["uvx", "--from", OTS_CLIENT, "ots", "verify",
                     str(proof)], capture_output=True, text=True, timeout=120)
                blob = (out.stderr + out.stdout).lower()
                if "success" in blob or "bitcoin block" in blob:
                    r.note(f"ots verify {proof.name}: attested")
                elif "pending" in blob:
                    r.note(f"ots verify {proof.name}: pending (not yet in a block)")
                else:
                    r.warn(f"ots verify {proof.name}: {out.stderr.strip()[:120]}")
            except Exception as exc:  # noqa: BLE001 - report, never crash the audit
                r.warn(f"ots verify {proof.name} could not run: {exc}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", default="es")
    ap.add_argument("--all", action="store_true", help="every market under Data/")
    ap.add_argument("--verify-ots", action="store_true",
                    help="also shell `ots verify` on each stamped proof — the ONLY mode that asserts Bitcoin confirmation (slow, network)")
    args = ap.parse_args()

    if args.all:
        # every market is Data/<slug>/ now (ES included) — just glob; the old
        # ["es"] + … prefix would double-count es after the Stage-B move.
        slugs = sorted(p.parent.name for p in DATA.glob("*/receipts.jsonl"))
    else:
        slugs = [args.market]

    any_fail = False
    for slug in slugs:
        rep = verify_market(slug, args.verify_ots)
        print(f"\n=== {slug.upper()} — {rep.checked} settlements re-derived ===")
        for m in rep.info:
            print(f"  info  {m}")
        for m in rep.warns:
            print(f"  WARN  {m}")
        for m in rep.fails:
            print(f"  FAIL  {m}")
        status = "FAIL" if rep.fails else ("clean" if not rep.warns else "clean (warnings)")
        print(f"  -> {status}: {len(rep.fails)} fail, {len(rep.warns)} warn")
        any_fail |= bool(rep.fails)

    print(f"\n{'DISCREPANCY FOUND' if any_fail else 'ALL CHECKS PASSED'}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
