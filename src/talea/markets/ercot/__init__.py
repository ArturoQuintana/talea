"""ERCOT (Texas) — DAM Settlement Point Prices, hub HB_NORTH, USD. Redistributable
(NP4-190 public raw-data carve-out) but NOT yet public (no mirror/dashboard). DAM
gate 10:00 CT, America/Chicago. driver="actions": ERCOT DAM geo-blocks EU IPs, so
this market is driven from GitHub Actions US runners, not the Hetzner server.

Migrated into a vertical `markets/<slug>/` module in Phase 2 (2026-08-28), matching
DE/FR: owns the ERCOT MIS client (`markets/ercot/fetch.py`) + presentation.
`talea.fetch_ercot` stays a compat re-export shim (the `ercot.yml` workflow
and the fetcher tests keep working).
"""
from __future__ import annotations

from ..base import Market, Presentation
from .fetch import make_fetch

MARKET = Market.make("ercot", "America/Chicago", make_fetch("HB_NORTH"),
                     deadline_hour=10, currency="USD", driver="actions",
                     public=True, redistributable=True,
                     presentation=Presentation(
                         title="ERCOT (HB_NORTH) day-ahead battery arbitrage",
                         tab_name="ERCOT", tz_label="Chicago",
                         source="ERCOT public data (NP4-190, DAM SPP)",
                         publication="the DAM publishes (~13:30 CT)"))
