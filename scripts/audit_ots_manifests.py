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

A manifest dated TODAY is exempt: `ots stamp` can legitimately not have run
yet, or its own weekly upgrade (Sun 12:00) may still be pending — that is
normal, not a gap.

ACCEPTED GAPS (allowlist): the 13 ES `<date>-2.txt` manifests for
2026-08-09..2026-08-21 were RATIFIED by the user 2026-08-29 as a permanent,
disclosed gap (do NOT mint; VERIFY.md STATUS line). Without an allowlist they
fired this alarm every Sunday, so a genuinely NEW unstamped manifest was
indistinguishable from the weekly noise (infra-audit 2026-11-01, F3). Exactly
those 13 names are exempt — nothing else, and the set is closed: extending it
is a user decision, not a code change.

Run: uv run python scripts/audit_ots_manifests.py
Exit 0 = every non-today manifest is stamped; exit 1 = gaps found (printed).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

OTS_DIR = Path(__file__).resolve().parents[1] / "Data" / "es" / "ots"  # ES at Data/es/ (Stage B)

# Ratified 2026-08-29 (docs/incidents.md): accepted permanent disclosure.
ACCEPTED_UNSTAMPED = frozenset(f"2026-08-{d:02d}-2.txt" for d in range(9, 22))


def find_unstamped(ots_dir: Path, today: str,
                   accepted: frozenset[str] = ACCEPTED_UNSTAMPED) -> list[str]:
    """Return sorted names of *.txt manifests with no sibling *.txt.ots,
    excluding today's (proof may not have been submitted yet) and the
    ratified accepted gaps."""
    gaps = []
    for manifest in sorted(ots_dir.glob("*.txt")):
        if manifest.name.startswith(today) or manifest.name in accepted:
            continue
        if not manifest.with_suffix(".txt.ots").exists():
            gaps.append(manifest.name)
    return gaps


def main() -> int:
    if not OTS_DIR.exists():
        print("[ots-audit] Data/es/ots absent; nothing to check")
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    gaps = find_unstamped(OTS_DIR, today)
    if not gaps:
        print("[ots-audit] every non-today manifest is stamped")
        return 0
    print(f"[ots-audit] {len(gaps)} manifest(s) written but never stamped "
          "(no Bitcoin anchor for that file state):")
    for name in gaps:
        print(f"  Data/es/ots/{name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
