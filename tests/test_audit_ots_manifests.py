"""Regression for the 2026-08-24 audit finding: Data/ots/2026-08-10-2.txt
(and 12 sibling dates) hold the real, final settlement state but were never
submitted to OpenTimestamps, so 13 dates' Bitcoin anchor covers only an
earlier, partial intra-day state. scripts/audit_ots_manifests.py must flag
any written-but-unstamped manifest so this class never goes unnoticed again.
Since 2026-11-11 it must do so in every market's Data/<slug>/ots, not ES alone.
"""
import importlib.util
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "audit_ots_manifests",
    Path(__file__).resolve().parents[1] / "scripts" / "audit_ots_manifests.py",
)
audit_ots_manifests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_ots_manifests)


def test_flags_manifest_written_without_a_matching_proof(tmp_path):
    (tmp_path / "2026-08-10.txt").write_text("stamped state\n")
    (tmp_path / "2026-08-10.txt.ots").write_bytes(b"proof")
    (tmp_path / "2026-08-10-2.txt").write_text("displaced, never stamped\n")

    # empty allowlist: this exact name is now ratified, but the raw class
    # detection must still work.
    gaps = audit_ots_manifests.find_unstamped(tmp_path, today="2026-08-24",
                                              accepted=frozenset())

    assert gaps == ["2026-08-10-2.txt"]


def test_todays_manifest_is_exempt_even_if_not_yet_stamped(tmp_path):
    (tmp_path / "2026-08-24.txt").write_text("just written\n")

    gaps = audit_ots_manifests.find_unstamped(tmp_path, today="2026-08-24")

    assert gaps == []


def test_fully_stamped_directory_has_no_gaps(tmp_path):
    (tmp_path / "2026-08-09.txt").write_text("state\n")
    (tmp_path / "2026-08-09.txt.ots").write_bytes(b"proof")

    gaps = audit_ots_manifests.find_unstamped(tmp_path, today="2026-08-24")

    assert gaps == []


def test_manifest_dated_after_utc_today_is_exempt(tmp_path):
    """A market ahead of UTC (JP) names its manifest for a day UTC has not
    reached yet; it is as fresh as today's and may still be stamped."""
    (tmp_path / "2026-11-12.txt").write_text("JP, written 15:13 UTC on 11-11\n")
    (tmp_path / "2026-11-10.txt").write_text("yesterday, never stamped\n")

    gaps = audit_ots_manifests.find_unstamped(tmp_path, today="2026-11-11")

    assert gaps == ["2026-11-10.txt"]


# --- T-05 (2026-11-11): the audit ran on ES only; every market stamps ---

def _market(data, slug, stamped=(), unstamped=(), receipts=True, ots=True):
    d = data / slug
    d.mkdir(parents=True)
    if receipts:
        (d / "receipts.jsonl").write_text("{}\n")
    if ots:
        (d / "ots").mkdir()
        for name in stamped:
            (d / "ots" / name).write_text("state\n")
            (d / "ots" / f"{name}.ots").write_bytes(b"proof")
        for name in unstamped:
            (d / "ots" / name).write_text("never stamped\n")


def test_gap_in_a_market_other_than_es_is_found(tmp_path):
    """The class T-05 closes: ES clean, another market holding an unstamped
    manifest. The ES-only audit reported this tree as clean."""
    _market(tmp_path, "es", stamped=["2026-11-01.txt"])
    _market(tmp_path, "gb", stamped=["2026-11-01.txt"], unstamped=["2026-11-02.txt"])
    _market(tmp_path, "jp", stamped=["2026-11-01.txt"])

    gaps, unanchored = audit_ots_manifests.audit_markets(tmp_path, "2026-11-11")

    assert gaps == {"es": [], "gb": ["2026-11-02.txt"], "jp": []}
    assert unanchored == []


def test_es_allowlist_does_not_excuse_the_same_name_in_another_market(tmp_path):
    name = RATIFIED[0]
    _market(tmp_path, "es", unstamped=[name])
    _market(tmp_path, "de", unstamped=[name])

    gaps, _ = audit_ots_manifests.audit_markets(tmp_path, "2026-11-11")

    assert gaps == {"de": [name], "es": []}


def test_market_with_receipts_but_no_ots_directory_is_flagged(tmp_path):
    _market(tmp_path, "es", stamped=["2026-11-01.txt"])
    _market(tmp_path, "nl", ots=False)                      # receipts, never anchored
    _market(tmp_path, "calibration", receipts=False, ots=False)   # not a market
    (tmp_path / "OTS-GAPS.log").write_text("a file, not a directory\n")

    gaps, unanchored = audit_ots_manifests.audit_markets(tmp_path, "2026-11-11")

    assert gaps == {"es": []}
    assert unanchored == ["nl"]


def test_main_no_data_dir_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit_ots_manifests, "DATA_DIR", tmp_path / "absent")
    assert audit_ots_manifests.main() == 0
    assert "absent" in capsys.readouterr().out


def test_main_all_stamped_returns_zero_and_names_the_markets(tmp_path, monkeypatch, capsys):
    _market(tmp_path, "es", stamped=["2020-01-01.txt"])     # past date, not today
    _market(tmp_path, "gb", stamped=["2020-01-01.txt"])
    monkeypatch.setattr(audit_ots_manifests, "DATA_DIR", tmp_path)
    assert audit_ots_manifests.main() == 0
    out = capsys.readouterr().out
    assert "2 market(s) checked (es, gb)" in out
    assert "every manifest dated before today is stamped" in out


def test_main_reports_gaps_with_their_market_and_returns_one(tmp_path, monkeypatch, capsys):
    _market(tmp_path, "es", stamped=["2020-01-01.txt"])
    _market(tmp_path, "pt", unstamped=["2020-01-01.txt"])   # no .ots sibling
    monkeypatch.setattr(audit_ots_manifests, "DATA_DIR", tmp_path)
    assert audit_ots_manifests.main() == 1
    out = capsys.readouterr().out
    assert "1 manifest(s) written but never stamped" in out
    assert "Data/pt/ots/2020-01-01.txt" in out
    assert "Data/es/" not in out


def test_main_reports_an_unanchored_market_and_returns_one(tmp_path, monkeypatch, capsys):
    _market(tmp_path, "es", stamped=["2020-01-01.txt"])
    _market(tmp_path, "nl", ots=False)
    monkeypatch.setattr(audit_ots_manifests, "DATA_DIR", tmp_path)
    assert audit_ots_manifests.main() == 1
    out = capsys.readouterr().out
    assert "1 market(s) with receipts but no ots/ directory" in out
    assert "Data/nl/" in out
    assert "never stamped" not in out


# --- infra-audit 2026-11-01 F3: the alarm was saturated by the ratified gap ---

RATIFIED = [f"2026-08-{d:02d}-2.txt" for d in range(9, 22)]


def test_allowlist_is_exactly_the_13_ratified_manifests():
    assert audit_ots_manifests.ACCEPTED_UNSTAMPED == frozenset(RATIFIED)
    assert len(audit_ots_manifests.ACCEPTED_UNSTAMPED) == 13


def test_ratified_gap_alone_does_not_fire(tmp_path):
    for name in RATIFIED:
        (tmp_path / name).write_text("accepted, never stamped\n")
    assert audit_ots_manifests.find_unstamped(tmp_path, today="2026-11-01") == []


def test_new_unstamped_manifest_still_fires_beside_the_ratified_gap(tmp_path):
    for name in RATIFIED:
        (tmp_path / name).write_text("accepted, never stamped\n")
    (tmp_path / "2026-08-22-2.txt").write_text("NEW gap, adjacent date\n")
    (tmp_path / "2026-10-30.txt").write_text("NEW gap\n")
    gaps = audit_ots_manifests.find_unstamped(tmp_path, today="2026-11-01")
    assert gaps == ["2026-08-22-2.txt", "2026-10-30.txt"]


def test_allowlist_has_no_stale_entries():
    """Every allowlisted name is a real ES manifest that is in fact unstamped:
    an entry that later got a proof, or names a file that does not exist, is a
    standing licence for nothing and must be removed.

    Deliberately NOT 'the live tree has no other gap': that is the weekly
    audit's job (it logs to Data/OTS-GAPS.log). A unit test that read live data
    for that would turn main red, on every later push, because of one missed
    OTS stamp — a data event, not a code defect. (Changed from the first
    version of this resolver PR after an independent review, 2026-11-10.)"""
    ots = audit_ots_manifests.OTS_DIR
    if not ots.exists():
        return
    for name in sorted(audit_ots_manifests.ACCEPTED_UNSTAMPED):
        assert (ots / name).exists(), f"allowlisted {name} is not in {ots}"
        assert not (ots / name).with_suffix(".txt.ots").exists(), \
            f"allowlisted {name} now HAS a proof — drop it from the allowlist"
