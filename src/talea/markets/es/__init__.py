"""Spain (ES) — the primary market (apidatos.ree.es, public, Bitcoin-anchored; its
receipt drives the heartbeat). Gate 13:00 Madrid.

Migrated into a vertical `markets/es/` module in Phase 2 (2026-08-28), the last
incumbent. STAGE A: ES keeps the repo-root `Data/` paths (not `Data/es/`) for
continuity — `markets/es/fetch.py` owns the apidatos client, but the audit-trail
paths and `loop._default_market` are unchanged. The `Data/ -> Data/es/` move that
finally makes ES market-neutral is STAGE B (docs/es-data-migration-plan.md).

Presentation strings are byte-for-byte what the registry held — the public page
must not change.
"""
from __future__ import annotations

from ..base import Market, Presentation
from ...loop import LEDGER, MARKET_TZ, PRICES, RECEIPTS
from .fetch import fetch_hourly

MARKET = Market("es", MARKET_TZ, 13, "EUR", PRICES, RECEIPTS, LEDGER, fetch_hourly,
                primary=True, public=True, redistributable=True,
                presentation=Presentation(
                    title="Spanish day-ahead battery arbitrage",
                    tab_name="Spain", tz_label="Madrid", show_gate=True,
                    source="apidatos.ree.es,\n      cross-checked weekly against "
                           "the independent token ESIOS route",
                    publication="the D+1 auction publishes (~13:15 CET)"))
