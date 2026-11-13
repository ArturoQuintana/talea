"""Market CONFORMANCE contract — the integrity cure (adopted 2026-08-28).

The one harm that matters for this project is a market going live MISSING a guard:
IT/PT shipped 2026-08-28 without a deadline fixture test. This battery is
parametrized over the LIVE registry, so a newly-registered market that lacks any
guard FAILS CI rather than committing broken. Adding a market cannot half-onboard
it — every market here is checked for the same guards, automatically.

Scope: CORRECTNESS-of-config + the clock guard (static, hermetic). LIVENESS — did
every market that should commit actually commit? — is a RUNTIME concern (a silent
stop is invisible to a unit test); that is Pillar B, the staleness monitor, not
this file.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import talea.loop as loop
from talea import markets as reg
from talea.loop import tick

MARKETS = list(reg.MARKETS.values())
IDS = [m.slug for m in MARKETS]


def _flat_day(d: str, base: float = 60.0, cheap=(3, 4), dear=(20, 21)) -> dict[str, float]:
    prices = [base] * 24
    for h in cheap:
        prices[h] = base - 50
    for h in dear:
        prices[h] = base + 50
    return {f"{d}T{h:02d}": p for h, p in enumerate(prices)}


def test_exactly_one_primary():
    """The heartbeat and every 'the primary' consumer assume a unique primary."""
    assert sum(1 for m in MARKETS if m.primary) == 1


@pytest.mark.parametrize("m", MARKETS, ids=IDS)
def test_market_config_invariants(m):
    assert 0 <= m.deadline_hour <= 23, f"{m.slug} deadline_hour out of range"
    assert m.currency and m.currency.isupper() and len(m.currency) == 3, \
        f"{m.slug} currency not a 3-letter code"
    assert callable(m.fetch), f"{m.slug} has no fetch callable wired"
    # LICENSE: you may only PUBLICLY mirror data you are allowed to redistribute.
    # This is the guard against an IT/PT-style price-redistribution leak.
    if m.public:
        assert m.redistributable, \
            f"{m.slug} is public but not redistributable — license-leak risk"
        assert m.presentation.title and m.presentation.tz_label, \
            f"{m.slug} is public but its presentation is incomplete (title/tz_label)"
    # PATH ISOLATION: a non-primary market lives entirely under Data/<slug>/.
    if not m.primary:
        assert m.slug in str(m.receipts_path.parent), \
            f"{m.slug} receipts are not isolated under its own dir"
        assert m.slug in str(m.ledger_path.parent), \
            f"{m.slug} ledger is not isolated under its own dir"


@pytest.mark.parametrize("m", MARKETS, ids=IDS)
def test_clock_guard_fires_at_each_market_deadline(m, tmp_path):
    """Every registered market's clock guard must REFUSE a commit at/after its
    deadline in ITS OWN timezone, and ALLOW one before. Generalizes the IT/PT
    detector to the whole registry: PT's Europe/Lisbon runs an hour behind
    Rome/Madrid, so its 12:00-local deadline is a distinct wall-clock instant —
    and so is any future market's (GB 11:00 London, JEPX 10:00 JST, ...). A market
    whose deadline is mis-wired against its auction is the classic leak path; this
    makes it impossible to add one silently."""
    day = _flat_day("2026-08-01")
    common = dict(fetch=lambda a, b: day, today=date(2026, 8, 1), sleep=lambda _s: None)

    # AT/after the deadline (market-local): clock guard refuses, no receipt.
    late_mkt = loop.Market.make(m.slug, m.tz, fetch=None, deadline_hour=m.deadline_hour,
                                currency=m.currency, root=tmp_path / f"{m.slug}_late")
    s_no = tick(market=late_mkt,
                now_fn=lambda: datetime(2026, 8, 1, m.deadline_hour, 30, tzinfo=late_mkt.tz),
                **common)
    assert s_no["committed"] == [], f"{m.slug} committed past its deadline"
    assert any("clock guard" in x for x in s_no["skipped"]), \
        f"{m.slug} clock guard did not fire at its {m.deadline_hour}:00 deadline"

    # Before the deadline: the same state commits fine (only meaningful when the
    # deadline leaves an earlier same-day hour, i.e. deadline_hour > 0).
    if m.deadline_hour > 0:
        early_mkt = loop.Market.make(m.slug, m.tz, fetch=None, deadline_hour=m.deadline_hour,
                                     currency=m.currency, root=tmp_path / f"{m.slug}_early")
        s_ok = tick(market=early_mkt,
                    now_fn=lambda: datetime(2026, 8, 1, m.deadline_hour - 1, 5, tzinfo=early_mkt.tz),
                    **common)
        assert s_ok["committed"], f"{m.slug} refused a commit before its deadline"


# ---- the record itself: no receipt was ever committed at or after its deadline ----

DATA = loop.DATA_DIR


def _receipts_past_deadline(path, market) -> tuple[list[int], list[int], list[int], list[int]]:
    """Line NUMBERS (from 1) of the receipts in `path` that: were committed at or
    after `market`'s deadline hour; were committed on another day than the day
    before their TARGET; cannot be read; or name a basis day that is not the
    day before their target. Times are the market's own.

    The day a receipt had to be committed on is derived from its target, never
    taken from its own `basis_day`: a line that claimed a later basis day would
    otherwise be judged against the day it chose. A stamp with no zone is
    unreadable, not the local time of whatever machine runs the test."""
    import json
    from datetime import timedelta
    late, other_day, unreadable, wrong_basis = [], [], [], []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            stamp = datetime.fromisoformat(rec["committed_at"])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("no zone")
            at = stamp.astimezone(market.tz)
            day = (date.fromisoformat(rec["target"]) - timedelta(days=1)).isoformat()
            basis = rec["basis_day"]
        except (ValueError, KeyError, TypeError):
            unreadable.append(n)
            continue
        if basis != day:
            wrong_basis.append(n)
        if at.date().isoformat() != day:
            other_day.append(n)
        elif at.hour >= market.deadline_hour:
            late.append(n)
    return late, other_day, unreadable, wrong_basis


@pytest.mark.parametrize("market", MARKETS, ids=IDS)
def test_every_receipt_in_the_record_was_committed_before_its_deadline(market):
    """The detector for the class found on 2026-11-13: the clock guard judged
    the start of a pass, so a pass that began before the deadline and wrote
    after it could have left a receipt stamped past the deadline. The fix is in
    `tick()`; this reads what is actually on disk. Every line of every market's
    receipts must have a `committed_at` that, in the market's own time, falls
    on the day before the receipt's target and before the market's deadline
    hour; and its `basis_day` must be that same day.

    It reads the files at test time and holds no line of them: this folder is
    copied to a public repository. A failure names the file and the line
    NUMBER only. A market whose folder is absent (a public copy without the
    private markets) is skipped. If a market's deadline is ever moved earlier,
    its older receipts need a dated rule here, not a looser one.

    If this fails on main, `test` is red and the red-main alert tells the
    operator: a receipt at or after its deadline is a claim about the record,
    to be disclosed, never edited away (the files are append-only)."""
    path = DATA / market.slug / "receipts.jsonl"
    if not path.exists():
        pytest.skip(f"no receipts file for {market.slug} in this checkout")
    late, other_day, unreadable, wrong_basis = _receipts_past_deadline(path, market)
    where = f"Data/{market.slug}/receipts.jsonl"
    assert not unreadable, (f"{where}: line(s) {unreadable} have no readable target, basis_day "
                            "or zoned committed_at")
    assert not wrong_basis, f"{where}: line(s) {wrong_basis} name a basis day that is not the day before their target"
    assert not other_day, f"{where}: line(s) {other_day} were not committed on the day before their target"
    assert not late, (f"{where}: line(s) {late} were committed at or after the "
                      f"{market.deadline_hour}:00 {market.tz.key} deadline")


def test_the_detector_finds_a_late_receipt_and_names_only_its_line(tmp_path):
    """The detector must be able to fail. A file with one receipt of each kind,
    for a market whose deadline is 10:00 in Tokyo (09:00 JST is 00:00 UTC)."""
    import json
    jp = reg.MARKETS["jp"]
    def rec(stamp, basis="2026-09-01", target="2026-09-02"):
        return json.dumps({"target": target, "basis_day": basis, "committed_at": stamp})
    path = tmp_path / "receipts.jsonl"
    path.write_text("\n".join([
        rec("2026-09-01T00:59:59.999999+00:00"),      # 1: 09:59:59 JST, in time
        rec("2026-09-01T01:00:00+00:00"),              # 2: 10:00:00 JST, the gate itself
        rec("2026-09-01T03:00:00+00:00"),              # 3: 12:00 JST, late
        rec("2026-08-31T16:00:00+00:00"),              # 4: 01:00 JST on 09-01: the right day, in time
        rec("2026-09-01T16:00:00+00:00"),              # 5: 01:00 JST on 09-02: the target day itself
        "",                                            # 6: blank, ignored
        json.dumps({"target": "2026-09-02", "basis_day": "2026-09-01"}),   # 7: no stamp
        rec("not a time"),                             # 8: unreadable
        "{torn",                                       # 9: not JSON
        # 10: claims a basis day AFTER its target and is stamped before the gate on that day
        rec("2026-09-02T16:30:00+00:00", basis="2026-09-03"),
        rec("2026-09-01T09:00:00"),                    # 11: a stamp with no zone
        rec("2026-09-01T00:30:00+00:00", basis="2026-08-31"),   # 12: in time, but the wrong basis day
        json.dumps({"basis_day": "2026-09-01", "committed_at": "2026-09-01T00:30:00+00:00"}),  # 13: no target
    ]) + "\n")
    late, other_day, unreadable, wrong_basis = _receipts_past_deadline(path, jp)
    assert late == [2, 3]
    assert other_day == [5, 10]
    assert unreadable == [7, 8, 9, 11, 13]
    assert wrong_basis == [10, 12]
