"""Great Britain (GB) — Elexon BMRS Market Index (APXMIDP leg), REDISTRIBUTABLE
(Elexon Insights open data — same public tier as ES/DE). Launched silent-first
2026-08-28, public since the licence evidence was confirmed.

WHAT THE FEED ACTUALLY IS (corrected 2026-09-07, independent-audit finding F2):
the Elexon Market Index Price is a TRADED WITHIN-DAY REFERENCE INDEX (the volume-
weighted price of APX short-term trades per half-hour) that populates
PROGRESSIVELY THROUGH THE DELIVERY DAY — it is NOT the GB day-ahead auction
clearing price (EPEX SPOT / Nord Pool N2EX own those; their channels are
licence-restricted). Verified from the git history of Data/gb/prices.json: after
every tick the last stored hour is ~1h before the tick (09:00Z tick -> T09,
10:30Z -> T11, 15:00Z -> T15). Consequences, all disclosed on the public page
(presentation.note) and in VERIFY.md:
  * The leak guard is unaffected: at the 09:00Z tick NO price for target T (=
    tomorrow) exists, so a receipt for T is honestly pre-commitment; T settles
    once its 24 hours have all arrived (the day after).
  * `battery-2h2h-persistence` (the panel's PRIMARY) needs yesterday's COMPLETE
    profile at the pre-deadline tick, which this feed cannot provide (today's
    hours are still arriving; the strategy's basis_fn asks for the day before
    the target, i.e. TODAY — incomplete). It therefore never commits here, and
    `rankblend` (needs both legs) never commits either. Only climatology and
    weekly produce GB receipts. This is a structural property of the feed, not
    an outage.
  * Whether GB should move to a genuine day-ahead source, or keep this framing,
    is a scope decision escalated to the user (2026-09-07) — not changed here.

The earlier docstring's "prices publish ~10:00 UTC" was wrong (it described a
day-ahead auction this feed is not). deadline_hour=11 (London) remains the clock
backstop; the dataset-relative leak guard is the real gate.

Dedicated client (`markets/gb/fetch.py`, the BMRS Market Index parser; chunks the
window to Elexon's 7-day cap). First market added on the finished plugin
architecture (2026-08-28); the shortlist's #1 (public anchor, open-licence).
"""
from __future__ import annotations

from ..base import Market, Presentation
from .fetch import fetch_hourly

MARKET = Market.make("gb", "Europe/London", fetch_hourly,
                     deadline_hour=11, currency="GBP",
                     public=True, redistributable=True,
                     presentation=Presentation(
                         title="GB day-ahead battery arbitrage",
                         tab_name="Great Britain", tz_label="London",
                         source="Elexon BMRS Market Index (Insights, open data)",
                         publication=("the first Market Index price for the "
                                      "target day exists (the index populates "
                                      "through the delivery day itself)"),
                         note=("Settlement price is the Elexon BMRS Market Index "
                               "(APXMIDP leg): a traded within-day reference "
                               "index that arrives progressively during the "
                               "delivery day, not the GB day-ahead auction "
                               "clearing price. Receipts are still committed "
                               "before any price for the target day exists "
                               "(leak-guarded). Because yesterday's profile is "
                               "never complete at the pre-deadline tick, the "
                               "Persistence v1 primary (and Rank-blend v1, "
                               "which needs it) structurally never commit in "
                               "this market — only Climatology v1 and Weekly v1 "
                               "do. See VERIFY.md.")))
