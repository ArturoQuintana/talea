"""Japan (JP) — JEPX day-ahead spot (SYSTEM price). REDISTRIBUTABLE (attribution-
required per JEPX terms — public-eligible like GB/DE), launching SILENT-FIRST
(public=False) pending a confirming email to JEPX; redistributable=True records the
open posture.

driver="actions": JEPX publishes ~10:10 JST (~01:10 UTC) — the European night, when
the Hetzner server does not tick — so JP is driven by a GitHub Actions workflow
(.github/workflows/jp.yml) timed BEFORE the 10:00 JST gate, the same pattern ERCOT
uses for its geo-block. Asia/Tokyo, JPY (¥/MWh after the fetcher's ¥/kWh × 1000).
deadline_hour=10 (Tokyo): the Actions retry ladder (four rungs from just after JST
midnight, see jp.yml) commits before the 10:00 gate; the dataset-relative leak guard
is the real gate.

Dedicated client (markets/jp/fetch.py, the JEPX spot CSV parser). Japan has NO DST
(48 slots/day). Third market of the shortlist's 'Now' batch (GB, FR, JP).
"""
from __future__ import annotations

from ..base import Market, Presentation
from .fetch import fetch_hourly

MARKET = Market.make("jp", "Asia/Tokyo", fetch_hourly,
                     deadline_hour=10, currency="JPY",
                     public=False, redistributable=True, driver="actions",
                     presentation=Presentation(
                         title="Japan (JEPX) day-ahead battery arbitrage",
                         tab_name="Japan", tz_label="Tokyo",
                         source="JEPX spot market (system price; open data, attribution)"))
