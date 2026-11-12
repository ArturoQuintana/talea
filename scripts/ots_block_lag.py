#!/usr/bin/env python3
"""When was each receipt's Bitcoin block mined — before its prices published?

VERIFY.md's strong check says an OpenTimestamps proof shows a manifest "existed
at time X". X is the time of the Bitcoin block the proof ends in, and that block
is mined some time AFTER the stamp is submitted. A red-team review (2026-10-01,
finding WEAK 3a) pointed out that nobody had measured the gap, so nobody knew
whether X fell before or after the publication the receipt is meant to predate.
This script measures it from the audit files alone, per receipt:

  1. receipts.jsonl is append-only, so every manifest's `sha256(receipts.jsonl)`
     equals the hash of some line-prefix of today's file. That tells which
     receipts each manifest covers.
  2. Each upgraded proof names the Bitcoin block(s) it ends in; the height is
     read straight from the `.ots` file (no OpenTimestamps client needed).
  3. A receipt's anchor is the EARLIEST block among the manifests covering it.
     The block's time comes from a public block explorer.
  4. That time is compared with the reference instant for the receipt's target
     day: the market's nominal publication time (table REFERENCE below).

Receipts committed BEFORE a market's first manifest are counted apart: stamping
did not exist yet, so their only anchor is that first manifest, mined after
their prices published. That tier is disclosed in VERIFY.md section 1 (a
timestamp cannot be backdated) and is not a finding here.

What the figures do and do not show. Block times are set by miners and are only
loosely bound to real time (consensus tolerates about two hours ahead), and the
publication times are nominal. A margin of hours is solid; a margin of minutes
is not, either way. The script checks nothing cryptographic: `ots verify`
against a Bitcoin node is still the proof. It needs network access to the
explorer; without it the result is INCONCLUSIVE (exit 2), never "clean".

Run: uv run python scripts/ots_block_lag.py            (all public markets)
     uv run python scripts/ots_block_lag.py --market de --rows
Exit 0 = no receipt is anchored at or after its reference instant, other than
the disclosed dates (DISCLOSED_LATE, listed in VERIFY.md section 1);
exit 1 = an undisclosed one was found (printed); exit 2 = explorer unreachable.

Stdlib-only, like verify_ledger.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).resolve().parents[1] / "Data"
# Two independent public explorers with the same (Esplora) API. The second is
# used when the first refuses or is unreachable.
EXPLORERS = ("https://blockstream.info/api", "https://mempool.space/api")

# OpenTimestamps' tag for a Bitcoin block-header attestation; it is followed by
# the payload length and the block height, both as LEB128 varints.
BITCOIN_TAG = bytes.fromhex("0588960d73d71901")

# The instant a receipt for target day T must predate, per market:
# (timezone, days before T, local time, what it is). GB's feed is not an
# auction (VERIFY.md): its first price for T is the first half hour of T itself.
REFERENCE = {
    "es": ("Europe/Madrid", 1, time(13, 15), "day-ahead publication, ~13:15 Madrid"),
    "de": ("Europe/Berlin", 1, time(12, 40), "day-ahead publication, ~12:40 Berlin"),
    "ercot": ("America/Chicago", 1, time(13, 30), "DAM publication, ~13:30 Chicago"),
    "gb": ("Europe/London", 0, time(0, 0), "start of the target day, London"),
}

# Receipts whose earliest block is at or after the reference instant, each
# disclosed in VERIFY.md section 1. (market, target day). The set is closed:
# a new one is a finding to disclose, not a line to add here quietly.
DISCLOSED_LATE = frozenset({
    ("es", "2026-08-09"),   # first day of ES stamping; proof submitted late that day
    ("de", "2026-08-28"),   # first day of DE stamping
    ("de", "2026-10-11"),   # block mined minutes after the nominal 12:40 Berlin
})


class Row(NamedTuple):
    target: str
    committed: datetime
    height: int
    block: datetime
    reference: datetime

    @property
    def lag_min(self) -> float:          # commit -> block
        return (self.block - self.committed).total_seconds() / 60

    @property
    def margin_min(self) -> float:       # block -> reference; negative = late
        return (self.reference - self.block).total_seconds() / 60


# --- pure ---------------------------------------------------------------------

def _varuint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7


def block_heights(proof: bytes) -> list[int]:
    """Bitcoin block heights attested in an .ots proof; [] while it is pending."""
    heights, i = [], proof.find(BITCOIN_TAG)
    while i != -1:
        try:
            _, j = _varuint(proof, i + len(BITCOIN_TAG))      # payload length
            height, _ = _varuint(proof, j)
            heights.append(height)
        except IndexError:                                     # truncated file
            pass
        i = proof.find(BITCOIN_TAG, i + 1)
    return sorted(heights)


def prefix_hashes(receipts: bytes) -> dict[str, int]:
    """sha256 of every whole-line prefix of receipts.jsonl -> its line count."""
    out, h, n = {}, hashlib.sha256(), 0
    for line in receipts.splitlines(keepends=True):
        h.update(line)
        n += 1
        out[h.copy().hexdigest()] = n
    return out


def manifest_receipts_hash(text: str) -> str | None:
    m = re.search(r"sha256\(receipts\.jsonl\)=([0-9a-f]{64})", text)
    return m.group(1) if m else None


def reference_instant(slug: str, target: str) -> datetime:
    tz, days_before, at, _ = REFERENCE[slug]
    day = date.fromisoformat(target) - timedelta(days=days_before)
    return datetime.combine(day, at, ZoneInfo(tz)).astimezone(timezone.utc)


class Measurement(NamedTuple):
    rows: list[Row]          # one per target day committed since stamping began
    unanchored: int          # receipts no upgraded proof covers yet
    pending: int             # proofs still waiting for their Bitcoin block
    pre_stamping: int        # receipts committed before the first manifest
    first_stamp: date | None


def first_stamp_day(ots_dir: Path) -> date | None:
    """The day of the market's first manifest (files are named <date>[-n].txt)."""
    days = [date.fromisoformat(m.name[:10]) for m in ots_dir.glob("*.txt")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(-\d+)?\.txt", m.name)]
    return min(days) if days else None


def coverage(market_dir: Path) -> tuple[list[dict], dict[int, int], int]:
    """(receipts, {line number -> earliest attested height}, pending proofs).

    A manifest covering n lines anchors receipts 1..n; each receipt keeps the
    lowest height among the manifests that cover it."""
    raw = (market_dir / "receipts.jsonl").read_bytes()
    receipts = [json.loads(line) for line in raw.splitlines() if line.strip()]
    prefixes = prefix_hashes(raw)
    covers: list[tuple[int, int]] = []          # (lines covered, earliest height)
    pending = 0
    for manifest in sorted((market_dir / "ots").glob("*.txt")):
        proof = manifest.with_suffix(".txt.ots")
        if not proof.exists():
            continue
        heights = block_heights(proof.read_bytes())
        if not heights:
            pending += 1
            continue
        lines = prefixes.get(manifest_receipts_hash(manifest.read_text()) or "")
        if lines:
            covers.append((lines, heights[0]))
    earliest: dict[int, int] = {}
    for lines, height in covers:
        for n in range(1, lines + 1):
            if n not in earliest or height < earliest[n]:
                earliest[n] = height
    return receipts, earliest, pending


def measure(market_dir: Path, slug: str, block_time) -> Measurement:
    """One Row per target day (judged on its last-anchored receipt), with the
    receipts that fall outside the measurement counted, not dropped silently."""
    receipts, earliest, pending = coverage(market_dir)
    first = first_stamp_day(market_dir / "ots")
    by_target: dict[str, Row] = {}
    unanchored = pre_stamping = 0
    for n, r in enumerate(receipts, start=1):
        committed = datetime.fromisoformat(r["committed_at"])
        if first is not None and committed.astimezone(timezone.utc).date() < first:
            pre_stamping += 1
            continue
        if n not in earliest:
            unanchored += 1
            continue
        row = Row(r["target"], committed, earliest[n], block_time(earliest[n]),
                  reference_instant(slug, r["target"]))
        if r["target"] not in by_target or row.block > by_target[r["target"]].block:
            by_target[r["target"]] = row
    return Measurement([by_target[t] for t in sorted(by_target)], unanchored,
                       pending, pre_stamping, first)


def late(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.block >= r.reference]


def undisclosed(slug: str, rows: list[Row]) -> list[Row]:
    return [r for r in late(rows) if (slug, r.target) not in DISCLOSED_LATE]


def summarise(rows: list[Row]) -> dict:
    lags = sorted(r.lag_min for r in rows)
    on_time = sorted(r.margin_min for r in rows if r.block < r.reference)
    return {"targets": len(rows), "first": rows[0].target, "last": rows[-1].target,
            "lag_median": lags[len(lags) // 2], "lag_max": lags[-1],
            "late": len(late(rows)),
            "under_1h": sum(1 for m in on_time if m < 60),
            "tightest": on_time[0] if on_time else None}


# --- effects ------------------------------------------------------------------

class Explorer:
    """Block time by height from an Esplora-style API. Paced, because public
    explorers answer 429 to a burst (a full run asks about 700 questions): a
    short pause before every request, a longer wait after a 429, and the next
    explorer in the list when one keeps refusing — which then stays the one in
    use, so a rate-limited explorer is not asked again for every block."""
    def __init__(self, bases: tuple[str, ...] = EXPLORERS, *, pause: float = 0.2,
                 waits: tuple[float, ...] = (5, 20), _open=urllib.request.urlopen,
                 _sleep=_time.sleep):
        self.bases = tuple(b.rstrip("/") for b in bases)
        self.pause, self.waits = pause, waits
        self._open, self._sleep, self._cache = _open, _sleep, {}
        self._start = 0                       # index of the explorer in use

    def _get(self, path: str) -> str:
        last: OSError | None = None
        for k in range(len(self.bases)):
            i = (self._start + k) % len(self.bases)
            base = self.bases[i]
            for wait in (0, *self.waits):
                self._sleep(wait or self.pause)
                req = urllib.request.Request(base + path,
                                             headers={"User-Agent": "ots-block-lag"})
                try:
                    with self._open(req, timeout=30) as resp:
                        self._start = i
                        return resp.read().decode()
                except urllib.error.HTTPError as exc:
                    last = exc
                    if exc.code != 429:
                        break               # not a rate limit: try the next explorer
                except OSError as exc:
                    last = exc
                    break
        raise last

    def block_time(self, height: int) -> datetime:
        if height not in self._cache:
            block_hash = self._get(f"/block-height/{height}").strip()
            stamp = json.loads(self._get(f"/block/{block_hash}"))["timestamp"]
            self._cache[height] = datetime.fromtimestamp(stamp, timezone.utc)
        return self._cache[height]


def report(slug: str, m: Measurement, show_rows: bool) -> None:
    rows, unanchored, pending = m.rows, m.unanchored, m.pending
    s = summarise(rows)
    print(f"{slug}: {s['targets']} target days {s['first']} to {s['last']} "
          f"({REFERENCE[slug][3]})")
    print(f"  commit to block: median {s['lag_median']:.0f} min, "
          f"longest {s['lag_max'] / 60:.1f} h")
    tight = "n/a" if s["tightest"] is None else f"{s['tightest']:.0f} min"
    print(f"  block at or after the reference instant: {s['late']} day(s); "
          f"under one hour before it: {s['under_1h']}; tightest margin {tight}")
    print(f"  not yet anchored: {unanchored} receipt(s); proofs pending: {pending}")
    if m.pre_stamping:
        print(f"  committed before stamping began ({m.first_stamp}): "
              f"{m.pre_stamping} receipt(s) — no pre-publication anchor is "
              "possible for them (VERIFY.md section 1)")
    for r in rows:
        is_late = r.block >= r.reference
        if show_rows or is_late:
            note = ""
            if is_late:
                note = ("  LATE (disclosed)" if (slug, r.target) in DISCLOSED_LATE
                        else "  LATE — NOT DISCLOSED")
            print(f"    target {r.target}  committed {r.committed:%m-%d %H:%MZ}  "
                  f"block {r.height} {r.block:%m-%d %H:%MZ}  reference "
                  f"{r.reference:%m-%d %H:%MZ}  margin {r.margin_min / 60:+.2f} h{note}")


def main(argv: list[str] | None = None, *, explorer: Explorer | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--market", action="append", choices=sorted(REFERENCE),
                    help="market to measure (repeatable; default: all present)")
    ap.add_argument("--rows", action="store_true", help="print every target day")
    args = ap.parse_args(argv)
    explorer = explorer or Explorer()
    slugs = args.market or [s for s in REFERENCE if (DATA_DIR / s / "ots").is_dir()]
    if not slugs:
        print("[ots-lag] no market with an ots/ directory; nothing to measure")
        return 0
    findings = 0
    for slug in slugs:
        try:
            m = measure(DATA_DIR / slug, slug, explorer.block_time)
        except OSError as exc:      # urllib's errors are OSErrors: no network, 4xx/5xx
            print(f"[ots-lag] INCONCLUSIVE for {slug}: {type(exc).__name__}: "
                  f"{str(exc)[:120]}")
            return 2
        if not m.rows:
            print(f"{slug}: no receipt has a Bitcoin anchor yet")
            continue
        report(slug, m, args.rows)
        findings += len(undisclosed(slug, m.rows))
    if findings:
        print(f"[ots-lag] {findings} receipt day(s) anchored at or after the "
              "reference instant and NOT disclosed in VERIFY.md")
        return 1
    print("[ots-lag] no undisclosed late anchor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
