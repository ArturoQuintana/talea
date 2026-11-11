"""Contract tests for the four market fetchers' fetch_hourly ORCHESTRATION —
the layer the parser unit-tests don't reach: chunking, weekly-bucket selection,
the MIS listing parse + zip download, and the ENTSO-E UTC-window request.

Each test injects a REAL recorded response (tests/fixtures/, captured
2026-08-20/27) through the fetcher's `_open` seam and pins the exact hourly
dict. These are regression + schema-documentation contracts: if an upstream
provider's real shape (as recorded) stops parsing, or a refactor breaks the
orchestration, the build fails. (Live upstream drift is a separate concern —
that needs a scheduled smoke test, not a fixture.)
"""
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from talea.markets import _entsoe as fetch_entsoe
from talea.markets.de import fetch as fetch_smard
from talea.markets.ercot import fetch as fetch_ercot
from talea.markets.es import fetch

FIX = Path(__file__).resolve().parent / "fixtures"


class _Resp:
    def __init__(self, data: bytes):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def router(mapping: dict[str, bytes], calls: list):
    """A urlopen seam that returns recorded bytes by URL-substring match and
    records every URL it was asked for (so tests can assert what was fetched)."""
    def _open(url, timeout=None):
        calls.append(url)
        for needle, data in mapping.items():
            if needle in url:
                return _Resp(data)
        raise AssertionError(f"unexpected fetch: {url}")
    return _open


# ---- Spain (apidatos) --------------------------------------------------------

def test_es_apidatos_hourly_means_from_quarter_hour_values():
    raw = (FIX / "apidatos_es_2026-08-20.json").read_bytes()
    calls = []
    out = fetch.fetch_hourly(date(2026, 8, 20), date(2026, 8, 20),
                             _open=router({"apidatos.ree.es": raw}, calls))
    assert len(out) == 24
    # mean of the hour's 4 quarters: (201.42+198.52+190.49+187.95)/4 = 194.595
    # exactly, a genuine two-decimal rounding boundary. Which way round() lands
    # depends on the LSB of the summed float, which itself depends on the
    # interpreter's float-sum algorithm: CPython 3.12+ sums floats with
    # Neumaier compensation (bpo-100425) and gets exactly 778.38 -> 194.59;
    # 3.11 and earlier sum naively and get 778.3800000000001 -> 194.6. Both are
    # correct roundings of their own (different, equally valid) intermediate
    # float; assert the interpreter-independent invariant instead of pinning
    # one side of a boundary this fixture happens to sit on.
    assert out["2026-08-20T00"] in (194.59, 194.6)
    assert out["2026-08-20T12"] == 50.41
    assert out["2026-08-20T23"] == 173.01
    assert len(calls) == 1                       # single chunk for one day


def test_es_chunks_ranges_over_28_days():
    raw = (FIX / "apidatos_es_2026-08-20.json").read_bytes()
    calls = []
    fetch.fetch_hourly(date(2026, 8, 20), date(2026, 9, 25),   # 37 days
                       _open=router({"apidatos.ree.es": raw}, calls))
    assert len(calls) == 2                       # 28-day chunk boundary
    calls.clear()
    fetch.fetch_hourly(date(2026, 8, 20), date(2026, 9, 10),   # 22 days
                       _open=router({"apidatos.ree.es": raw}, calls))
    assert len(calls) == 1


# ---- Germany (SMARD) ---------------------------------------------------------

def test_de_smard_selects_overlapping_bucket_only():
    index = (FIX / "smard_index_hour.json").read_bytes()
    bucket = (FIX / "smard_4169_DE_hour_1786917600000.json").read_bytes()
    calls = []
    out = fetch_smard.fetch_hourly(
        date(2026, 8, 20), date(2026, 8, 20),
        _open=router({"index_hour": index, "hour_1786917600000": bucket}, calls))
    assert len(out) == 24
    assert out["2026-08-20T00"] == 172.71
    assert out["2026-08-20T12"] == 83.63
    # index + exactly ONE weekly bucket (the one covering the day)
    series_calls = [c for c in calls if "4169_DE_hour_" in c]
    assert len(series_calls) == 1


# ---- Portugal / Italy (ENTSO-E A44) ------------------------------------------

def test_pt_entsoe_a44_aggregates_quarter_hours(monkeypatch):
    monkeypatch.setenv("ENTSOE_TOKEN", "test-token")   # _open is mocked anyway
    from talea.markets.pt import PT, LISBON
    xml = (FIX / "entsoe_a44_pt_2026-08-20.xml").read_bytes()
    calls = []
    fetch_pt = fetch_entsoe.make_fetch(PT, LISBON,
                                       _open=router({"web-api.tp.entsoe.eu": xml}, calls))
    out = fetch_pt(date(2026, 8, 20), date(2026, 8, 20))
    assert len(out) == 24
    # The fixture is SYNTHETIC (prices from a formula, see its header): expected
    # values were computed by hand from that formula, not read off the parser.
    # T00 = period 2, positions 5-8 -> mean(54.75, 53.5, 55.0, 56.5)
    assert out["2026-08-20T00"] == 54.94
    # T12 = period 2, positions 53-56, where 53 and 55 are OMITTED (curveType A03:
    # carry the previous price) -> mean(78.5, 78.75, 78.75, 79.0)
    assert out["2026-08-20T12"] == 78.75
    # T23 = period 3, positions 1-4 -> mean(61.5, 63.0, 61.75, 63.25)
    assert out["2026-08-20T23"] == 62.38
    assert "documentType=A44" in calls[0] and "securityToken=test-token" in calls[0]
    assert f"in_Domain={PT}" in calls[0]        # targets the Portugal zone (EIC guard)


def _not_synthetic(text: str) -> bool:
    """True if `text` is an ENTSO-E document that is not marked synthetic."""
    is_entsoe = "iec62325" in text or "Publication_MarketDocument" in text
    return is_entsoe and "<mRID>synthetic-fixture</mRID>" not in text


def test_entsoe_fixtures_are_synthetic():
    """Incident 2026-11-11: a real ENTSO-E A44 response for Portugal (three days
    of 15-minute prices) sat in tests/fixtures/ and was published on the public
    mirror from late August, because publish_mirror.sh syncs tests/ whole and the
    data allowlist only guards Data/. ENTSO-E prices are not freely
    redistributable (IT/PT/FR are private for that reason). Any ENTSO-E document
    kept as a fixture must be synthetic and say so in its mRID."""
    for f in sorted(FIX.iterdir()):
        text = f.read_text(errors="replace")
        assert not _not_synthetic(text), (
            f"{f.name} is an ENTSO-E document not marked synthetic — tests/ is "
            f"published; replace its prices with generated ones")
    # the guard must be able to fail the way the real defect did
    real = ('<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:'
            'publicationdocument:7:3"><mRID>13482adfbeb94275</mRID>')
    assert _not_synthetic(real)
    assert not _not_synthetic(real.replace("13482adfbeb94275", "synthetic-fixture"))
    assert not _not_synthetic('{"series": [[1786917600000, 83.63]]}')   # not ENTSO-E


def test_fr_entsoe_targets_the_france_zone(monkeypatch):
    """FR must query the FRANCE bidding zone, not accidentally IT/PT — the classic
    copy-paste-the-wrong-EIC bug that would silently onboard a mislabeled market.
    Guards markets/fr/'s EIC + Europe/Paris wiring against the shared A44 client."""
    monkeypatch.setenv("ENTSOE_TOKEN", "test-token")
    from talea.markets.fr import FR, PARIS
    assert FR == "10YFR-RTE------C" and str(PARIS) == "Europe/Paris"
    xml = (FIX / "entsoe_a44_pt_2026-08-20.xml").read_bytes()   # A44 parse is zone-agnostic
    calls = []
    fetch_fr = fetch_entsoe.make_fetch(FR, PARIS,
                                       _open=router({"web-api.tp.entsoe.eu": xml}, calls))
    out = fetch_fr(date(2026, 8, 20), date(2026, 8, 20))
    assert f"in_Domain={FR}" in calls[0] and f"out_Domain={FR}" in calls[0]
    assert "documentType=A44" in calls[0]
    assert len(out) >= 20, "FR fetch parsed no Paris-local hours"


# ---- ERCOT (MIS listing -> zip -> CSV) ---------------------------------------

def _ercot_zip() -> bytes:
    csv_text = (
        "DeliveryDate,HourEnding,SettlementPoint,SettlementPointPrice\n"
        "08/27/2026,01:00,HB_NORTH,42.10\n"
        "08/27/2026,02:00,HB_NORTH,38.55\n"
        "08/27/2026,01:00,HB_HOUSTON,44.00\n"
        "08/27/2026,24:00,HB_NORTH,50.25\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cdr.00012331.DAMSPNP4190_csv.csv", csv_text)
    return buf.getvalue()


def test_ercot_listing_parse_download_unzip_pipeline():
    listing = (FIX / "ercot_listing_12331.html").read_bytes()
    calls = []
    fetch_north = fetch_ercot.make_fetch(
        "HB_NORTH",
        _open=router({"GetReports.do": listing, "doclookupId=": _ercot_zip()}, calls))
    out = fetch_north(date(2026, 8, 27), date(2026, 8, 27))
    # HourEnding 01:00/02:00/24:00 -> local hours 0/1/23, HB_NORTH only
    assert out == {"2026-08-27T00": 42.10, "2026-08-27T01": 38.55,
                   "2026-08-27T23": 50.25}
    # listing fetched once; only 2026-08-27-delivery zips downloaded (not all 31)
    assert sum(1 for c in calls if "GetReports.do" in c) == 1
    downloads = sum(1 for c in calls if "doclookupId=" in c)
    assert 1 <= downloads <= 3      # posting revisions of the same delivery day


def test_gb_bmrs_market_index_targets_elexon():
    """GB fetch must hit Elexon's open BMRS Market Index endpoint (NOT EPEX/Nord
    Pool directly, which are licence-restricted) and parse the APXMIDP leg."""
    from talea.markets.gb import fetch as gb
    import json as _json
    payload = _json.dumps({"data": [
        {"dataProvider": "APXMIDP", "price": 80.0, "startTime": "2026-08-19T23:00:00Z"},
        {"dataProvider": "APXMIDP", "price": 90.0, "startTime": "2026-08-19T23:30:00Z"},
    ]}).encode()
    calls = []
    out = gb.fetch_hourly(date(2026, 8, 20), date(2026, 8, 20),
                          _open=router({"data.elexon.co.uk": payload}, calls))
    assert "data.elexon.co.uk" in calls[0] and "market-index" in calls[0]
    assert "format=json" in calls[0]
    assert out["2026-08-20T00"] == 85.0        # (80+90)/2, London hour 00


def test_gb_chunks_windows_to_elexon_7day_cap():
    """Incident 2026-08-29: GB 400'd on EVERY scheduled tick. A market with no seed
    prices fetches ~7 months from loop.py's default start in ONE request, but Elexon
    caps the market-index from->to range at 7 days inclusive ("must not exceed 7
    days"). fetch_hourly MUST split any window into <=7-day chunks — a single
    over-limit request is exactly the bug. (It only ever 'worked' in a hand-run
    2-day smoke test, which is one chunk.)"""
    from talea.markets.gb import fetch as gb
    from datetime import datetime as _dt, timedelta as _td
    import json as _json, re
    calls = []
    gb.fetch_hourly(date(2026, 8, 1), date(2026, 8, 30),           # 30 days
                    _open=router({"data.elexon.co.uk": _json.dumps({"data": []}).encode()}, calls))
    assert len(calls) == 5                          # 6-day chunks -> ceil(30/6)
    for url in calls:                               # NONE may exceed Elexon's cap
        frm = _dt.fromisoformat(re.search(r"from=([^&]+)", url).group(1).replace("Z", "+00:00"))
        to = _dt.fromisoformat(re.search(r"to=([^&]+)", url).group(1).replace("Z", "+00:00"))
        assert (to - frm) <= _td(days=7), f"chunk exceeds Elexon 7-day cap: {url}"


def test_gb_settlementperiod_fallback_and_null_price_skip():
    """Some Elexon records lack startTime and carry settlementDate +
    settlementPeriod instead (period 1 = 00:00–00:30 London); parse_market_index
    must fall back to that. A null price is SKIPPED, never treated as 0 (which
    would silently drag the hourly mean down). Covers the parser's fallback +
    null-guard branches."""
    from talea.markets.gb.fetch import parse_market_index
    payload = {"data": [
        # no startTime -> settlementDate/period fallback; periods 1 & 2 = London hour 00
        {"dataProvider": "APXMIDP", "price": 70.0, "settlementDate": "2026-08-20", "settlementPeriod": 1},
        {"dataProvider": "APXMIDP", "price": 80.0, "settlementDate": "2026-08-20", "settlementPeriod": 2},
        # null price (period 3 = hour 01) -> skipped, so hour 01 gets no key
        {"dataProvider": "APXMIDP", "price": None, "settlementDate": "2026-08-20", "settlementPeriod": 3},
        # wrong provider -> ignored
        {"dataProvider": "N2EXMIDP", "price": 0.0, "settlementDate": "2026-08-20", "settlementPeriod": 1},
    ]}
    out = parse_market_index(payload, date(2026, 8, 20), date(2026, 8, 20))
    assert out["2026-08-20T00"] == 75.0        # (70+80)/2, periods 1&2; N2EXMIDP ignored
    assert "2026-08-20T01" not in out          # only a null price there -> no key, not 0


def test_jp_fetch_targets_jepx_csv_with_referer():
    """JP must fetch the JEPX spot CSV with the required Referer header (the server
    returns 0 bytes without it) and the fiscal-year filename, then convert the
    system price ¥/kWh -> ¥/MWh."""
    from talea.markets.jp import fetch as jp
    import io
    csv = ("受渡日,時刻コード,売り,買い,約定,システムプライス(円/kWh)\n"
           "2026/08/27,1,1,1,1,10.00\n2026/08/27,2,1,1,1,12.00\n").encode()
    seen = {}
    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["referer"] = req.get_header("Referer")
        return io.BytesIO(csv)
    out = jp.fetch_hourly(date(2026, 8, 27), date(2026, 8, 27), _open=fake_open)
    assert "csv_read.php" in seen["url"] and "spot_summary_2026.csv" in seen["url"]
    assert seen["referer"] == "https://www.jepx.jp/electricpower/market-data/spot/"
    assert out["2026-08-27T00"] == 11000.0
