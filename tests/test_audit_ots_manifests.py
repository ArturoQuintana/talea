"""Regression for the 2026-08-24 audit finding: Data/ots/2026-08-10-2.txt
(and 12 sibling dates) hold the real, final settlement state but were never
submitted to OpenTimestamps, so 13 dates' Bitcoin anchor covers only an
earlier, partial intra-day state. scripts/audit_ots_manifests.py must flag
any written-but-unstamped manifest so this class never goes unnoticed again.
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


def test_main_no_ots_dir_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit_ots_manifests, "OTS_DIR", tmp_path / "absent")
    assert audit_ots_manifests.main() == 0
    assert "absent" in capsys.readouterr().out


def test_main_all_stamped_returns_zero(tmp_path, monkeypatch, capsys):
    (tmp_path / "2020-01-01.txt").write_text("state\n")       # past date, not today
    (tmp_path / "2020-01-01.txt.ots").write_bytes(b"proof")
    monkeypatch.setattr(audit_ots_manifests, "OTS_DIR", tmp_path)
    assert audit_ots_manifests.main() == 0
    assert "every non-today manifest is stamped" in capsys.readouterr().out


def test_main_reports_gaps_and_returns_one(tmp_path, monkeypatch, capsys):
    (tmp_path / "2020-01-01.txt").write_text("never stamped\n")   # no .ots sibling
    monkeypatch.setattr(audit_ots_manifests, "OTS_DIR", tmp_path)
    assert audit_ots_manifests.main() == 1
    out = capsys.readouterr().out
    assert "written but never stamped" in out and "2020-01-01.txt" in out


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
