"""Drift-proof tests for the market registry (Phase 0 of the market-plugin
refactor). Before Phase 0 there were five hand-synced market lists and the
dashboard renderer had silently dropped PT and ERCOT. These assert every
registered market is reachable from every operational surface, so that class of
drift fails the build."""
import importlib.util
from pathlib import Path

from talea import markets as reg

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_exactly_one_primary_and_the_rest_are_shadows():
    assert sum(m.primary for m in reg.MARKETS.values()) == 1
    partition = {m.slug for m in reg.shadows()} | {
        m.slug for m in reg.MARKETS.values() if m.primary}
    assert partition == set(reg.MARKETS)


def test_query_helpers_match_the_flags():
    assert {m.slug for m in reg.public_markets()} == {"es", "de", "gb", "ercot"}
    assert {m.slug for m in reg.by_driver("actions")} == {"ercot", "jp"}
    assert {m.slug for m in reg.by_driver("server")} == {"de", "it", "pt", "fr", "gb"}
    # by_driver never returns the primary (the ES pass runs it directly)
    assert all(not m.primary for m in reg.by_driver("server"))


def test_render_dashboard_presentation_covers_every_market():
    # THE regression that motivated Phase 0: the renderer's market set is now
    # derived from the registry, so no market can be silently missing again.
    rd = _load_script("render_dashboard")
    assert set(rd.MARKETS) == set(reg.MARKETS)


def test_every_market_has_presentation_and_a_tz_label():
    for slug, m in reg.MARKETS.items():
        assert m.presentation.title, f"{slug} has no dashboard title"
        assert m.presentation.tz_label, f"{slug} has no tz label"
        assert m.presentation.tab_name, f"{slug} has no tab name"


def test_every_public_market_states_its_own_publication_label():
    """Audit note 2026-09-07: the DE and ERCOT public pages quoted Spain's
    '~13:15 CET' publication time because the renderer had one hardcoded clock.
    A PUBLIC market must carry its own `presentation.publication` label so a
    future public market cannot silently inherit another market's clock (the
    renderer's fallback is deliberately generic, never another market's time)."""
    for m in reg.public_markets():
        assert m.presentation.publication, \
            f"public market {m.slug!r} has no presentation.publication label"
        assert "13:15" not in m.presentation.publication or m.slug == "es", \
            f"{m.slug!r} quotes Spain's 13:15 publication time"


def test_gb_discloses_its_within_day_index_feed():
    """Audit finding F2 (2026-09-07): GB settles against the Elexon Market
    Index (APXMIDP), a traded within-day index that populates through the
    delivery day — not a day-ahead auction — so the persistence primary
    structurally never commits there. The registry entry must carry that
    disclosure (rendered on the public page) until the feed changes."""
    note = reg.MARKETS["gb"].presentation.note
    assert "Market Index" in note and "not the GB day-ahead auction" in note
    assert "Persistence v1" in note and "never commit" in note
