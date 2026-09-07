"""Coverage for the public dashboard renderer. It ships numbers to a public
page, so the contract is: every figure is computed correctly from the audit
files and lands in the HTML. We assert the Python-computed values (totals,
capture, wins, missed days, pair deltas, gate progress, market labels) — the
client-side SVG/JS is out of scope for unit tests. Failure-mode-first: the
missed-day and market-awareness cases are the ones a naive renderer gets wrong."""
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "render_dashboard",
    Path(__file__).resolve().parents[1] / "scripts" / "render_dashboard.py")
rd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rd)

P = "battery-2h2h-persistence"
C = "battery-2h2h-climatology"


def _prices(days):
    rows = []
    for d in days:
        for h in range(24):
            rows.append({"ts": f"{d}T{h:02d}", "price": 50.0 + h})
    return rows


def _settle(target, strat, pnl, oracle, cap, tau=0.9):
    return {"target": target, "strategy": strat, "strategy_version": "1",
            "buy_hours": [12, 13], "sell_hours": [21, 22],
            "buy_prices": [85.27, 83.27], "sell_prices": [175.44, 176.01],
            "pnl_eur": pnl, "oracle_pnl_eur": oracle, "capture": cap, "tau": tau}


def _receipt(target, basis, strat, committed="2026-08-12T09:04:00+00:00"):
    return {"target": target, "basis_day": basis, "strategy": strat,
            "strategy_version": "1", "buy_hours": [12, 13], "sell_hours": [21, 22],
            "committed_at": committed}


def _seed(monkeypatch, tmp_path, slug, days, ledger, receipts):
    d = tmp_path / slug
    d.mkdir(parents=True)
    (d / "prices.json").write_text(json.dumps(_prices(days)))
    (d / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))
    (d / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in receipts))
    monkeypatch.setitem(rd.MARKETS[slug], "data", d)
    return d


# ---- helpers -----------------------------------------------------------------

def test_fmt_thousands_and_two_decimals():
    assert rd.fmt(1234.5) == "1,234.50"
    assert rd.fmt(-3.1) == "-3.10"


def test_jsonl_missing_file_is_empty(tmp_path):
    assert rd.jsonl(tmp_path / "nope.jsonl") == []


def test_day_curves_builds_24_slot_arrays(tmp_path):
    (tmp_path / "prices.json").write_text(json.dumps(_prices(["2026-08-13"])))
    curves = rd.day_curves(tmp_path)
    assert list(curves) == ["2026-08-13"]
    assert len(curves["2026-08-13"]) == 24 and curves["2026-08-13"][5] == 55.0


# ---- build(): the numbers on the page ----------------------------------------

def test_build_es_core_numbers_and_gate(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 128.34, 151.82, 0.845),
              _settle("2026-08-13", C, 130.00, 151.82, 0.856)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P),
                _receipt("2026-08-13", "2026-08-12", C),
                _receipt("2026-08-14", "2026-08-13", P)]      # open (unsettled)
    _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"], ledger, receipts)
    html = rd.build("es")
    assert "+128.34" in html                         # primary total
    assert "151.82" in html and "84.5" in html       # oracle ceiling, mean capture
    assert "1 / 1" in html                           # 1 win of 1 settled
    assert '"target": "2026-08-13"' in html          # day-card JSON present
    assert "over 1 shared days" in html              # climatology-vs-primary pair note
    assert "GBM v2 gate" in html and "1 / 21" in html  # ES-only gate tile
    assert "Escalation gate" in html
    # the open 2026-08-14 receipt renders a pending card
    assert "Pending" in html and "2026-08-14" in html


def test_build_counts_missed_days(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 100.0, 120.0, 0.83),
              _settle("2026-08-15", P, 90.0, 110.0, 0.82)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P),
                _receipt("2026-08-15", "2026-08-14", P)]      # 08-14 has NO receipt
    _seed(monkeypatch, tmp_path, "es",
          ["2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15"], ledger, receipts)
    html = rd.build("es")
    assert "2 missed" not in html                    # exactly one gap
    assert "1 missed" in html or "· 1 missed" in html
    assert "missed — no receipt committed" in html   # the missed ledger row
    assert "2 / 2" in html                           # both settled days won


def test_build_missed_window_extends_to_last_receipt_not_last_settled(
        monkeypatch, tmp_path):
    """A commit gap AFTER the most recent settled day must surface immediately,
    not wait a settlement cycle. Only one day (08-28) has settled; receipts exist
    for 08-28 and 08-31 but NOT 08-29/08-30 (the tick didn't run). Bounding the
    missed-day window by the last SETTLED target (instead of the last RECEIPT
    target) hides this 2-day gap until 08-31 itself settles — the exact ERCOT
    under-reporting the independent auditor found 2026-08-31 ('0 missed' on a
    page with a real 2-day gap in receipts.jsonl)."""
    ledger = [_settle("2026-08-28", P, 50.0, 60.0, 0.83)]
    receipts = [_receipt("2026-08-28", "2026-08-27", P),
                _receipt("2026-08-31", "2026-08-30", P)]      # 08-29, 08-30 missing
    _seed(monkeypatch, tmp_path, "ercot",
          ["2026-08-27", "2026-08-28", "2026-08-30", "2026-08-31"], ledger, receipts)
    html = rd.build("ercot")
    assert "2 missed" in html
    assert "missed — no receipt committed" in html


def test_build_no_open_receipts_shows_none(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 100.0, 120.0, 0.83)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]      # all settled
    _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"], ledger, receipts)
    html = rd.build("es")
    assert "None open" in html


def test_build_de_is_market_aware_without_gate(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 200.0, 210.0, 0.95)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    _seed(monkeypatch, tmp_path, "de", ["2026-08-12", "2026-08-13"], ledger, receipts)
    html = rd.build("de")
    assert "German (DE-LU) day-ahead battery arbitrage" in html
    assert "Germany day-ahead ledger" in html        # tab title
    assert "SMARD.de" in html                        # source line
    assert "Escalation gate" not in html             # gate is ES-only
    assert "public ledger" in html and ">DE<" in html
    # regression: DE/GB/ERCOT are public markets (public=True in the registry);
    # the non-gate tile must never claim "silent" for a market the mirror
    # actually publishes (auditor finding, 2026-08-29).
    assert "silent" not in html.lower()


# ---- main(): file output -----------------------------------------------------

def test_main_writes_wrapped_html(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 100.0, 120.0, 0.83)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"], ledger, receipts)
    out = tmp_path / "index.html"
    monkeypatch.setattr(rd.sys, "argv", ["render_dashboard.py", str(out)])
    rd.main()
    text = out.read_text()
    assert text.startswith("<!doctype html>") and text.rstrip().endswith("</html>")
    assert "Spanish day-ahead battery arbitrage" in text


def test_main_market_flag_selects_de(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 200.0, 210.0, 0.95)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    _seed(monkeypatch, tmp_path, "de", ["2026-08-12", "2026-08-13"], ledger, receipts)
    out = tmp_path / "de.html"
    monkeypatch.setattr(rd.sys, "argv", ["render_dashboard.py", "--market", "de", str(out)])
    rd.main()
    assert "German (DE-LU) day-ahead battery arbitrage" in out.read_text()


# ---- P1: one-project mirror (currency, awaiting, nav, ES-at-index) -----------

def test_currency_symbol_is_per_market(monkeypatch, tmp_path):
    """A GBP market renders £, never €. A public page showing the wrong currency
    would undermine the credibility the record exists to establish."""
    ledger = [_settle("2026-08-13", P, 100.0, 120.0, 0.83)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    _seed(monkeypatch, tmp_path, "gb", ["2026-08-12", "2026-08-13"], ledger, receipts)
    html = rd.build("gb")
    assert "£" in html and "€" not in html
    assert "Absolute GBP is an" in html


def test_awaiting_page_when_no_settled_day(monkeypatch, tmp_path):
    """A live market with committed receipts but no settled day renders the honest
    'awaiting first settled day' page — never a crash, never a fake +0.00."""
    receipts = [_receipt("2026-08-14", "2026-08-13", P)]
    _seed(monkeypatch, tmp_path, "gb", ["2026-08-13"], [], receipts)   # empty ledger
    html = rd.build("gb")
    assert "awaiting first settled day" in html
    assert "Pending" in html and "2026-08-14" in html
    assert "+0.00" not in html


W = "battery-2h2h-weekly"


def test_shadow_settlements_are_never_hidden_behind_the_awaiting_page(
        monkeypatch, tmp_path):
    """Audit finding F1 (2026-09-07): GB's page said 'Live - awaiting first
    settled day' and labelled all 16 receipts 'Pending' while ledger.jsonl held
    12 settled shadow rows — build() switched to the awaiting template whenever
    the PRIMARY had no settlement, ignoring shadow settlements. Exact GB shape:
    climatology + weekly receipts for 3 targets, the first 2 settled, the
    primary has never committed anything. The page must show the settled
    shadow days with their P&L, mark ONLY the unsettled receipts pending, and
    disclose that the primary has never committed here."""
    receipts = [_receipt(t, b, s)
                for t, b in (("2026-08-31", "2026-08-30"), ("2026-09-01", "2026-08-31"),
                             ("2026-09-02", "2026-09-01"))
                for s in (C, W)]
    ledger = [_settle("2026-08-31", C, 116.06, 176.12, 0.659),
              _settle("2026-08-31", W, 65.50, 176.12, 0.372),
              _settle("2026-09-01", C, 240.51, 243.19, 0.989),
              _settle("2026-09-01", W, 240.51, 243.19, 0.989)]
    _seed(monkeypatch, tmp_path, "gb",
          ["2026-08-30", "2026-08-31", "2026-09-01"], ledger, receipts)
    html = rd.build("gb")
    assert "awaiting first settled day" not in html      # the contradiction is gone
    assert "2 settled days on shadow strategies" in html
    assert "+116.06" in html and "+240.51" in html       # settled shadow P&L shown
    assert "Climatology v1 · shadow" in html and "Weekly v1 · shadow" in html
    assert html.count(">Pending<") == 2                  # only the 09-02 pair
    assert "2026-09-02" in html
    assert "committed <b>no receipt</b> in this market so far (0 of 3 target days)" in html
    assert "+0.00" not in html                           # no fake primary total


def test_awaiting_page_only_when_nothing_at_all_has_settled(monkeypatch, tmp_path):
    """The awaiting template stays correct for its ONE honest case: an empty
    ledger. Shadow-only receipts with no settlement still render it."""
    receipts = [_receipt("2026-08-31", "2026-08-30", C)]
    _seed(monkeypatch, tmp_path, "gb", ["2026-08-30"], [], receipts)
    html = rd.build("gb")
    assert "awaiting first settled day" in html and html.count(">Pending<") == 1


def test_footer_publication_time_is_per_market(monkeypatch, tmp_path):
    """Audit note (2026-09-07): de.html and ercot.html footers quoted Spain's
    '~13:15 CET' as THEIR publication time (SMARD DE-LU is ~12:40-13:00 CET,
    ERCOT DAM ~13:30 CT) — the template had one hardcoded clock for every
    market. The footer must take the label from the market's own registry
    entry, on the full page AND the awaiting/shadow pages."""
    for slug in ("es", "de", "ercot", "gb"):
        _seed(monkeypatch, tmp_path, slug, ["2026-08-12", "2026-08-13"],
              [_settle("2026-08-13", P, 100.0, 120.0, 0.83)],
              [_receipt("2026-08-13", "2026-08-12", P)])
    es, de, ercot, gb = (rd.build(s) for s in ("es", "de", "ercot", "gb"))
    assert "13:15 CET" in es
    assert "13:15" not in de and "12:40-13:00 CET" in de
    assert "13:15" not in ercot and "13:30 CT" in ercot
    assert "13:15" not in gb and "Market Index" in gb
    # awaiting template (empty ledger) — same rule
    _seed(monkeypatch, tmp_path / "aw", "de", ["2026-08-12"], [],
          [_receipt("2026-08-13", "2026-08-12", P)])
    aw = rd.build("de")
    assert "awaiting first settled day" in aw and "13:15" not in aw \
        and "12:40-13:00 CET" in aw


def test_market_disclosure_note_renders_on_every_template(monkeypatch, tmp_path):
    """Audit finding F2 (2026-09-07): GB settles against the Elexon Market Index
    — a traded within-day index that arrives progressively, not a day-ahead
    auction — so the persistence primary structurally never commits there, and
    neither the page nor VERIFY.md said so. The registry's `presentation.note`
    must render verbatim as a Disclosure banner on the full, shadow-only and
    awaiting templates; markets with no note render no banner."""
    gb_note = rd.MARKETS["gb"]["note"]
    assert "Market Index" in gb_note and "never commit" in gb_note
    seeds = {
        "full": ([_settle("2026-08-13", P, 100.0, 120.0, 0.83)],
                 [_receipt("2026-08-13", "2026-08-12", P)]),
        "shadow": ([_settle("2026-08-13", C, 100.0, 120.0, 0.83)],
                   [_receipt("2026-08-13", "2026-08-12", C)]),
        "awaiting": ([], [_receipt("2026-08-13", "2026-08-12", C)]),
    }
    for name, (ledger, receipts) in seeds.items():
        _seed(monkeypatch, tmp_path / name, "gb", ["2026-08-12", "2026-08-13"],
              ledger, receipts)
        html = rd.build("gb")
        assert "<b>Disclosure.</b>" in html and gb_note in html, name
    _seed(monkeypatch, tmp_path, "de", ["2026-08-12", "2026-08-13"],
          *seeds["full"])
    assert "Disclosure." not in rd.build("de")


def test_nav_links_all_markets_and_marks_current(monkeypatch):
    nav = rd._nav(["es", "de", "gb"], "de", "es")
    assert ">Talea<" in nav
    assert 'href="index.html"' in nav              # primary es -> index.html
    assert 'href="de.html" class="here"' in nav    # current market marked
    assert 'href="gb.html"' in nav


def test_main_site_es_is_index_others_are_siblings(monkeypatch, tmp_path):
    """--site keeps the primary (ES) at index.html (NOT demoted to es.html) and
    writes each other public market as a first-class sibling page, all carrying
    the Talea nav. One project, no market subordinated, no 'benchmark' hub."""
    _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"],
          [_settle("2026-08-13", P, 100.0, 120.0, 0.90)],
          [_receipt("2026-08-13", "2026-08-12", P)])
    _seed(monkeypatch, tmp_path, "de", ["2026-08-12", "2026-08-13"],
          [_settle("2026-08-13", P, 200.0, 210.0, 0.95)],
          [_receipt("2026-08-13", "2026-08-12", P)])
    fake = [type("M", (), {"slug": s})() for s in ("es", "de")]
    monkeypatch.setattr(rd, "_public_markets", lambda: fake)
    out = tmp_path / "site"
    monkeypatch.setattr(rd.sys, "argv", ["render_dashboard.py", "--site", str(out)])
    rd.main()
    assert (out / "index.html").exists()           # ES at the root
    assert not (out / "es.html").exists()          # ES not demoted
    assert (out / "de.html").exists()
    idx = (out / "index.html").read_text()
    assert 'class="talea-nav"' in idx and "Spanish day-ahead battery arbitrage" in idx
    assert "German (DE-LU) day-ahead battery arbitrage" in (out / "de.html").read_text()


def test_pages_never_overclaim_bitcoin_finality(monkeypatch, tmp_path):
    """OTS proofs are PENDING (calendar-only) until a Bitcoin block confirms them
    (~days later). Every currently-public market except ES's pre-08-22 dates has
    only pending proofs, so a flat 'OpenTimestamps-anchored' claim overstates the
    timestamp's finality (red-team finding, 2026-08-29). The pages must say
    'stamped' and disclose the pending->confirmed distinction, never assert a
    Bitcoin anchor that does not yet exist. Covers BOTH templates (settled + the
    awaiting page a fresh market like GB renders)."""
    # settled ES page
    _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"],
          [_settle("2026-08-13", P, 100.0, 120.0, 0.83)],
          [_receipt("2026-08-13", "2026-08-12", P)])
    # awaiting GB page (committed receipt, no settled day, only-pending proof)
    _seed(monkeypatch, tmp_path, "gb", ["2026-08-13"], [],
          [_receipt("2026-08-14", "2026-08-13", P)])
    for slug, must_be_awaiting in (("es", False), ("gb", True)):
        html = rd.build(slug)
        assert "OpenTimestamps-anchored" not in html          # the overclaim is gone
        assert "OpenTimestamps-stamped" in html               # honest verb
        assert "within days" in html                          # confirmation is future
        if must_be_awaiting:
            assert "awaiting first settled day" in html       # right template


def test_day_card_skipped_when_basis_curve_missing(monkeypatch, tmp_path):
    # basis day 2026-08-12 has NO price row -> the day card is dropped, but the
    # settlement still appears in the append-only ledger table.
    ledger = [_settle("2026-08-13", P, 128.34, 151.82, 0.845)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    _seed(monkeypatch, tmp_path, "es", ["2026-08-13"], ledger, receipts)  # 08-12 absent
    html = rd.build("es")
    assert '"target": "2026-08-13"' not in html     # no card (basis missing)
    assert "+128.34" in html                         # but the ledger row stands


def test_day_card_skipped_when_an_hour_is_missing(monkeypatch, tmp_path):
    ledger = [_settle("2026-08-13", P, 128.34, 151.82, 0.845)]
    receipts = [_receipt("2026-08-13", "2026-08-12", P)]
    d = _seed(monkeypatch, tmp_path, "es", ["2026-08-12", "2026-08-13"], ledger, receipts)
    # drop one hour from the target day so its curve has a None
    rows = [r for r in json.loads((d / "prices.json").read_text())
            if r["ts"] != "2026-08-13T05"]
    (d / "prices.json").write_text(json.dumps(rows))
    html = rd.build("es")
    assert '"target": "2026-08-13"' not in html      # incomplete curve -> skipped
