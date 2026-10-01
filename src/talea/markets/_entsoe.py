"""Shared ENTSO-E Transparency A44 (day-ahead prices) LIBRARY. Stdlib-only.

A LIBRARY, not a market: it knows the A44 protocol and nothing about any specific
zone. Each ENTSO-E market (IT, PT, FR — all DERIVED-METRICS-ONLY: ENTSO-E day-ahead
is not freely redistributable, so raw prices are fetched for private use and never
republished) owns its bidding-zone EIC + timezone and calls `make_fetch(eic, tz)`.
This decouples those markets from one another — a change to Italy cannot touch
Portugal, because they no longer share a module (Phase-2 defect #3 fix, 2026-08-28).

The A44 document is a Publication_MarketDocument with one TimeSeries per delivery
day, each a Period of PT15M points (quarter-hourly since the 2025 MTU switch),
curveType A03 = variable block: a missing position carries the previous point's
price forward. Point positions map to UTC times from the Period start; we aggregate
the four quarter-hours of each local hour to an hourly mean, keyed "YYYY-MM-DDTHH"
local — the same hourly frame as every other market.

Auth: a free ENTSO-E security token in ENTSOE_TOKEN (env or repo .env).
"""
from __future__ import annotations

import os
import statistics
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# No market-specific constants live here: every ENTSO-E market (IT/PT/FR) owns its
# own EIC + timezone and passes them to make_fetch(). This is what makes them
# independent (Phase-2 defect #3 fix complete, 2026-08-28).
API = "https://web-api.tp.entsoe.eu/api"
ATTRIBUTION = "ENTSO-E Transparency Platform (private use; prices not redistributed)"


# The repo-root .env (gitignored, chmod 600 on the server) — the fallback when
# ENTSOE_TOKEN is not exported in the process environment (it never is under the
# systemd tick; the shell's `_env` reads the same file). Anchored on the REPO
# ROOT (src/talea/markets/_entsoe.py -> parents[3]), and pinned by
# tests/test_entsoe_token.py. The original `fetch_entsoe.py` lived one level up,
# where `parents[2]` WAS the repo root; the 2026-08-28 copy into markets/ kept
# the depth-relative lookup, which silently became src/.env — every IT/PT/FR
# fetch then failed "ENTSOE_TOKEN not set" for 33 days (incident 2026-10-01).
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _token() -> str:
    t = os.getenv("ENTSOE_TOKEN")
    if not t:
        envf = ENV_FILE
        if envf.exists():
            for line in envf.read_text().splitlines():
                if line.startswith("ENTSOE_TOKEN="):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not t:
        raise RuntimeError("ENTSOE_TOKEN not set (env or .env)")
    return t


def parse_a44(xml_text: str, tz: ZoneInfo, start: date, end: date) -> dict[str, float]:
    """A44 XML -> {"YYYY-MM-DDTHH": price} for Europe/Rome local days in
    [start, end]. Quarter-hour points (curveType A03: gaps carry the previous
    price forward) are aggregated to the hourly mean. Pure — unit-tested
    against a synthetic fixture of the real structure.

    DST: keyed by Rome local clock hour, so the autumn repeated hour collapses
    to one key (the documented 1-hour/year artifact shared by every market)."""
    root = ET.fromstring(xml_text)
    ns = {"n": root.tag.split("}")[0].strip("{")}
    hour_vals: dict[str, list[float]] = {}
    for series in root.findall(".//n:TimeSeries", ns):
        res_txt = series.find(".//n:resolution", ns).text        # e.g. PT15M
        step = int("".join(c for c in res_txt if c.isdigit()))   # minutes
        for period in series.findall("n:Period", ns):
            p_start = datetime.fromisoformat(
                period.find("n:timeInterval/n:start", ns).text.replace("Z", "+00:00"))
            p_end = datetime.fromisoformat(
                period.find("n:timeInterval/n:end", ns).text.replace("Z", "+00:00"))
            n = round((p_end - p_start).total_seconds() / 60 / step)
            present = {int(pt.find("n:position", ns).text):
                       float(pt.find("n:price.amount", ns).text)
                       for pt in period.findall("n:Point", ns)}
            last = None
            for pos in range(1, n + 1):
                last = present.get(pos, last)          # A03 fill-forward
                if last is None:
                    continue
                t_utc = p_start + timedelta(minutes=(pos - 1) * step)
                local = t_utc.astimezone(tz)
                if start <= local.date() <= end:
                    hour_vals.setdefault(local.strftime("%Y-%m-%dT%H"), []).append(last)
    return {k: round(statistics.fmean(v), 2) for k, v in hour_vals.items()}


def make_fetch(domain: str, tz: ZoneInfo, *, _open=urllib.request.urlopen):
    """Return a fetch_hourly(start, end) closure for one ENTSO-E bidding zone,
    matching the Market.fetch signature. Requests a UTC window covering the
    local days; the parser aggregates PT15M points to local hourly means and
    filters to local dates. Raises on network/HTTP errors."""
    def fetch_hourly(start: date, end: date) -> dict[str, float]:
        ps = datetime(start.year, start.month, start.day, tzinfo=tz) - timedelta(hours=2)
        pe = datetime(end.year, end.month, end.day, tzinfo=tz) + timedelta(days=1)
        url = (f"{API}?securityToken={_token()}&documentType=A44"
               f"&in_Domain={domain}&out_Domain={domain}"
               f"&periodStart={ps.astimezone(timezone.utc):%Y%m%d%H%M}"
               f"&periodEnd={pe.astimezone(timezone.utc):%Y%m%d%H%M}")
        with _open(url, timeout=60) as r:
            xml_text = r.read().decode("utf-8", "replace")
        return parse_a44(xml_text, tz, start, end)
    return fetch_hourly
