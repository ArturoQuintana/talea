"""Germany (DE-LU) — SMARD.de day-ahead spot, CC BY 4.0 (public: its own mirror +
dashboard). SDAC-coupled, gate 12:00 CET, Europe/Berlin.

Migrated into a vertical `markets/<slug>/` module in Phase 2 (2026-08-28), matching
the FR pilot: this module owns Germany's fetcher (`markets/de/fetch.py` = the SMARD
client) and presentation. `talea.fetch_smard` stays a compat re-export shim
so existing importers keep working.
"""
from __future__ import annotations

from ..base import Market, Presentation
from .fetch import fetch_hourly

MARKET = Market.make("de", "Europe/Berlin", fetch_hourly,
                     deadline_hour=12, currency="EUR", public=True,
                     redistributable=True,
                     presentation=Presentation(
                         title="German (DE-LU) day-ahead battery arbitrage",
                         tab_name="Germany", tz_label="Berlin",
                         source="Bundesnetzagentur | SMARD.de (CC BY 4.0)",
                         publication="the D+1 auction publishes (EPEX SPOT "
                                     "DE-LU via SMARD, ~12:40-13:00 CET)"))
