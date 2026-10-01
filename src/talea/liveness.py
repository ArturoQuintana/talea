"""Pillar B — the market LIVENESS assessment (pure; no I/O beyond reading
Data/<slug>/). Lifted out of scripts/check_liveness.py on 2026-10-01 so the
daily email digest can run the SAME assessment the server-side CLI runs: until
then the only liveness channel was an ntfy phone push from server_tick.sh —
ephemeral, unaudited — and IT/PT/FR stalled for 33 days while every digest kept
printing their last settled line unchanged (incident 2026-10-01). The CLI
(`scripts/check_liveness.py`) re-exports everything here; thresholds are the
pre-registered ones from the 2026-08-28 / 2026-09-02 incidents.

States per registered market:
- HEALTHY : a receipt within STALE_HOURS and prices not stale.
- STALE   : nothing committed in >STALE_HOURS, OR the newest stored price is
            older than PRICE_STALE_DAYS (fetcher stopped), OR the market has
            ticked NEVER_FETCH_TICKS+ days without ever storing a price.
- NEVER   : registered, zero receipts, fetch not provably broken (onboarding).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .loop import load_prices

STALE_HOURS = 48.0
PRICE_STALE_DAYS = 2   # a market whose NEWEST PRICE is older than this has a
# broken/stalled fetch. This catches the class the receipt-only check misses: a
# LAUNCHED market whose fetcher silently fails commits nothing, so it hides as the
# exempt NEVER even though it is actually broken (e.g. GB's Elexon 7-day-window
# 400s, 2026-08-29 — invisible to every check because a market with no data has
# nothing to verify AND no receipt to be "stale"). Stale prices are unambiguous:
# a working day-ahead fetch always advances to ~today/tomorrow.
NEVER_FETCH_TICKS = 3   # a market with >= this many dated OTS manifests (i.e. days
# of tick-ATTEMPTS) but STILL no price stored has a fetch that has been failing
# since launch — broken, not onboarding. This closes the gap the GB stale-price
# rule missed: JP's fetch was REJECTED every run (EUR-scaled sanity cap vs the
# ¥/MWh scale), so prices.json was NEVER populated -> no stale date to compare ->
# it hid as the exempt NEVER for a week (incident 2026-09-02).


def newest_price_date(prices_path: Path) -> date | None:
    """Newest local price date in a market's dataset, or None if it has none."""
    prices = load_prices(prices_path)
    return date.fromisoformat(max(prices)[:10]) if prices else None


def ots_attempt_days(ots_dir: Path | None) -> int:
    """Count dated OTS manifests — a proxy for how many days a market has been
    TICKING (a tick stamps a manifest even when the fetch is refused and nothing
    commits). Distinguishes a genuinely just-launched market (0-1) from one that
    has been attempting for days without ever storing a price."""
    if ots_dir is None or not ots_dir.exists():
        return 0
    return len(list(ots_dir.glob("*.txt")))


def newest_commit(receipts_path: Path) -> datetime | None:
    """The most recent committed_at across a market's receipts, or None if the
    market has never committed (missing file or empty)."""
    if not receipts_path.exists():
        return None
    stamps = []
    for line in receipts_path.read_text().splitlines():
        line = line.strip()
        if line:
            ca = json.loads(line).get("committed_at")
            if ca:
                stamps.append(datetime.fromisoformat(ca))
    return max(stamps) if stamps else None


def market_liveness(slug: str, receipts_path: Path, now: datetime,
                    stale_hours: float = STALE_HOURS,
                    prices_path: Path | None = None,
                    ots_dir: Path | None = None) -> dict:
    npd = newest_price_date(prices_path) if prices_path is not None else None
    price_age_d = (now.date() - npd).days if npd is not None else None
    fetch_stale = price_age_d is not None and price_age_d > PRICE_STALE_DAYS
    # never-populated: ticking for days (OTS manifests) yet NO price ever stored ->
    # the fetch has been failing/refused since launch, not onboarding (JP class).
    never_fetched = (prices_path is not None and npd is None
                     and ots_attempt_days(ots_dir) >= NEVER_FETCH_TICKS)
    broken = fetch_stale or never_fetched
    nc = newest_commit(receipts_path)
    if nc is None:
        # NEVER-committed: normally exempt (onboarding), BUT a broken fetch (stale
        # prices, or never-populated-despite-ticking) is not onboarding — it is STALE.
        return {"slug": slug, "last_commit": None, "age_h": None,
                "price_age_d": price_age_d, "fetch_stale": broken,
                "never_fetched": never_fetched,
                "state": "STALE" if broken else "NEVER"}
    age_h = (now - nc).total_seconds() / 3600.0
    stale = age_h > stale_hours or broken
    return {"slug": slug, "last_commit": nc, "age_h": age_h,
            "price_age_d": price_age_d, "fetch_stale": broken,
            "never_fetched": never_fetched,
            "state": "STALE" if stale else "HEALTHY"}


def assess(markets, now: datetime, stale_hours: float = STALE_HOURS) -> list[dict]:
    out = []
    for m in markets:
        pp = getattr(m, "prices_path", None)
        ots = getattr(m, "ots_dir", None) or ((pp.parent / "ots") if pp is not None else None)
        out.append(market_liveness(m.slug, m.receipts_path, now, stale_hours,
                                   prices_path=pp, ots_dir=ots))
    return out


def describe(r: dict) -> str:
    """One human line per market — shared by the CLI and the email digest."""
    if r["state"] == "NEVER":
        return f"  NEVER    {r['slug']:6} — no receipts yet"
    if r.get("never_fetched"):
        return (f"  STALE    {r['slug']:6} — never stored a price despite ticking "
                f"(fetch refused since launch?)")
    if r.get("fetch_stale"):
        return (f"  STALE    {r['slug']:6} — prices {r['price_age_d']}d stale "
                f"(fetcher stopped)")
    return f"  {r['state']:8} {r['slug']:6} — last commit {r['age_h']:.1f}h ago"
