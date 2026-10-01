"""Pillar B — market LIVENESS monitor (adopted 2026-08-28).

`verify_ledger` checks that the receipts that EXIST are correct. It is structurally
incapable of noticing a market that silently STOPPED committing — a dead systemd
timer, a broken fetcher, a revoked API key — because a market that writes nothing
has nothing to verify. That blind spot is the one integrity harm that matters: a
silently-dark market rots the benchmark while every other check stays green. This
monitor closes it.

For every REGISTERED market it measures hours since the newest `committed_at`:
- HEALTHY : a receipt within --stale-hours (default 48h; a daily market sits ~24h).
- STALE   : nothing in >threshold  → two consecutive missed days → the writer has
            stopped. Exit is non-zero so a caller can page.
- NEVER   : registered but zero receipts (onboarding, or a first receipt that never
            fired) → reported, but does NOT fail the exit (onboarding is expected).

Unlike `verify_ledger` (deliberately app-independent, disk-driven, checks
CORRECTNESS), this MUST read the registry — the whole point is to notice a market
that SHOULD be committing and isn't, which only the registry knows (COMPLETENESS).

Also surfaced in the daily email digest's ALERTS line (talea/liveness.py).
Run: uv run python scripts/check_liveness.py [--stale-hours 48] [--quiet]
Exit 1 if any registered market is STALE.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from talea import markets as registry  # noqa: E402
# The assessment itself lives in the package (talea/liveness.py, 2026-10-01) so
# the email digest shares it; re-exported here so this CLI and its tests keep
# their names (market_liveness, assess, newest_commit, ...).
from talea.liveness import (  # noqa: E402,F401
    NEVER_FETCH_TICKS, PRICE_STALE_DAYS, STALE_HOURS, assess, describe,
    market_liveness, newest_commit, newest_price_date, ots_attempt_days)

_fmt = describe   # back-compat alias


def main(argv: list[str] | None = None, market_list=None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description="Per-market liveness monitor.")
    ap.add_argument("--stale-hours", type=float, default=STALE_HOURS)
    ap.add_argument("--quiet", action="store_true", help="print only problems")
    a = ap.parse_args(argv)

    now = now or datetime.now(timezone.utc)
    markets = market_list if market_list is not None else list(registry.MARKETS.values())
    rows = assess(markets, now, a.stale_hours)
    stale = [r for r in rows if r["state"] == "STALE"]
    never = [r for r in rows if r["state"] == "NEVER"]

    for r in rows:
        if not a.quiet or r["state"] != "HEALTHY":
            print(describe(r))
    if stale:
        print(f"-> STALE (writer or fetcher stopped): "
              f"{', '.join(r['slug'] for r in stale)}")
    elif never:
        print(f"-> all live; NEVER (info): {', '.join(r['slug'] for r in never)}")
    else:
        print("-> all markets live")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
