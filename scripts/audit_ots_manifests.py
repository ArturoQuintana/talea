"""Detect OTS manifest slots that were written but never stamped.

Class this closes: the 2026-08-21 fix made stamped manifests immutable (a
changed audit trail gets a NEW suffixed slot instead of overwriting the
stamped one) but that only guarantees the *old* slot stays intact — it does
not guarantee every new slot actually gets stamped. A 2026-08-24 audit found
13 such gaps predating the fix (Data/ots/<date>-2.txt files holding the real,
final settlement state for 2026-08-09 through 2026-08-21, written during the
08-21 restoration but never submitted to OpenTimestamps) plus the structural
consequence: those 13 dates' Bitcoin anchor covers only an earlier, partial
intra-day state, not the final one. This script re-finds that condition so
it can never again go unnoticed for a full audit cycle.

EVERY MARKET (2026-11-11): the check ran on Data/es/ots only, although every
market has stamped its own Data/<slug>/ots since 2026-08-27..29 — an unstamped
manifest in any other market went unseen. It now walks every Data/<slug>/ that
has an ots/ directory. It also flags a market directory that holds receipts but
has NO ots/ directory at all: an audit trail that was never anchored (DE ran
that way 2026-08-24..26). The walk reads the directory tree, not the market
registry, so it runs the same from a bare clone of the public mirror.

A manifest dated TODAY (UTC) OR LATER is exempt: a failed stamp is retried by
that day's later ticks, so it is not yet a gap. Manifests are named for the
market's local day, and a market ahead of UTC (JP, and ES/DE late in the
evening) is already on tomorrow's date — hence "or later". A market behind UTC
(ERCOT) is still on yesterday's date between 00:00 UTC and its local midnight;
run in that window, the check can flag a manifest its market may still stamp.
The weekly run (Sun 11:00 UTC) is outside it.

ACCEPTED GAPS (allowlist): the 13 ES `<date>-2.txt` manifests for
2026-08-09..2026-08-21 were RATIFIED by the user 2026-08-29 as a permanent,
disclosed gap (do NOT mint; VERIFY.md STATUS line). Without an allowlist they
fired this alarm every Sunday, so a genuinely NEW unstamped manifest was
indistinguishable from the weekly noise (infra-audit 2026-11-01, F3). Exactly
those 13 names are exempt, in ES only — the same file name in another market
is a gap — and the set is closed: extending it is a user decision, not a code
change.

Run: uv run python scripts/audit_ots_manifests.py
Exit 0 = every manifest dated before today is stamped, in every market;
exit 1 = gaps found (printed, one path per line).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "Data"
OTS_DIR = DATA_DIR / "es" / "ots"  # ES at Data/es/ (Stage B); the allowlist's home

# Ratified 2026-08-29 (docs/incidents.md): accepted permanent disclosure.
ACCEPTED_UNSTAMPED = frozenset(f"2026-08-{d:02d}-2.txt" for d in range(9, 22))
# Per market: only ES has ratified gaps. A market not listed has none.
ACCEPTED_BY_MARKET = {"es": ACCEPTED_UNSTAMPED}


def find_unstamped(ots_dir: Path, today: str,
                   accepted: frozenset[str] = ACCEPTED_UNSTAMPED) -> list[str]:
    """Return sorted names of *.txt manifests with no sibling *.txt.ots,
    excluding those dated `today` (UTC, ISO) or later (the stamp may still be
    retried) and the ratified accepted gaps."""
    gaps = []
    for manifest in sorted(ots_dir.glob("*.txt")):
        if manifest.name[:10] >= today or manifest.name in accepted:
            continue
        if not manifest.with_suffix(".txt.ots").exists():
            gaps.append(manifest.name)
    return gaps


def has_receipts(path: Path) -> bool:
    """At least one receipt line: the file exists and is not blank."""
    return path.exists() and any(line.strip() for line in path.read_text().splitlines())


def audit_markets(data_dir: Path, today: str,
                  accepted: dict[str, frozenset[str]] | None = None,
                  ) -> tuple[dict[str, list[str]], list[str]]:
    """Walk every market directory under `data_dir`.

    Returns (gaps, unanchored): `gaps` maps each slug that has an ots/
    directory to its unstamped manifest names (empty list = clean);
    `unanchored` lists slugs that hold at least one receipt but have no ots/
    directory. A directory with neither (Data/calibration) is not a market,
    and neither is one whose receipts.jsonl is EMPTY: a pass refused at the
    moment of writing leaves the file it had opened, with nothing in it, and
    there is nothing there to anchor."""
    accepted = ACCEPTED_BY_MARKET if accepted is None else accepted
    gaps: dict[str, list[str]] = {}
    unanchored: list[str] = []
    for market_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        slug = market_dir.name
        if (market_dir / "ots").is_dir():
            gaps[slug] = find_unstamped(market_dir / "ots", today,
                                        accepted.get(slug, frozenset()))
        elif has_receipts(market_dir / "receipts.jsonl"):
            unanchored.append(slug)
    return gaps, unanchored


def main() -> int:
    if not DATA_DIR.exists():
        print("[ots-audit] Data/ absent; nothing to check")
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    gaps, unanchored = audit_markets(DATA_DIR, today)
    found = [(slug, name) for slug, names in gaps.items() for name in names]
    if not found and not unanchored:
        print(f"[ots-audit] {len(gaps)} market(s) checked "
              f"({', '.join(gaps) or 'none'}): every manifest dated before "
              "today is stamped")
        return 0
    if found:
        print(f"[ots-audit] {len(found)} manifest(s) written but never stamped "
              "(no Bitcoin anchor for that file state):")
        for slug, name in found:
            print(f"  Data/{slug}/ots/{name}")
    if unanchored:
        print(f"[ots-audit] {len(unanchored)} market(s) with receipts but no "
              "ots/ directory (audit trail never anchored):")
        for slug in unanchored:
            print(f"  Data/{slug}/")
    return 1


if __name__ == "__main__":
    sys.exit(main())
