"""The clock guard is judged at the moment a receipt is written.

Found in review, 2026-11-13: `tick()` read the clock once, at its start, then
fetched (Spain: with retry sleeps of 5 and 15 minutes), and only then applied
the clock guard, to that first reading. A pass that began before the deadline
and wrote after it would have produced a receipt whose stamp is past the
deadline, and possibly past publication. No stamp in the record is past its
deadline (see the detector in test_market_conformance.py); the closest stamp
is 3.4 seconds before its gate.

Every test here runs the real `tick()` with a clock on which time passes: the
injected `sleep`, and a `fetch` that takes time, move it forward.
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import talea.loop as loop
from talea.loop import tick

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 1)
PANEL = [s["strategy"] for s in loop.STRATEGIES]
RECEIPT_KEYS = ["target", "basis_day", "buy_hours", "sell_hours", "basis_profile",
                "strategy", "strategy_version", "params", "committed_at"]
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00$")


class Clock:
    """A clock on which time passes. Call it to read it; `sleep` moves it."""
    def __init__(self, start: datetime):
        self.t, self.reads = start, []

    def __call__(self) -> datetime:
        self.reads.append(self.t)
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _history(end: date, days: int = 35) -> dict[str, float]:
    """Complete days up to and including `end`: enough for every strategy of
    the panel to have a basis, so all four would commit."""
    prices = {}
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        for h in range(24):
            prices[f"{d}T{h:02d}"] = 60.0 + i + (-40 if h in (3, 4) else 40 if h in (20, 21) else 0)
    return prices


def _market(root: Path, *, tz="Europe/Madrid", deadline=13, retries=(300, 900), slug="es"):
    m = loop.Market.make(slug, tz, fetch=None, deadline_hour=deadline, currency="EUR",
                         root=root, fetch_retries=retries)
    loop.save_prices(_history(DAY), m.prices_path)
    return m


def _at(market, hour: int, minute: int, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=market.tz)


def _receipts(market) -> list[dict]:
    p = market.receipts_path
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _down(a, b):
    raise OSError("source does not answer")


# ---- 1. the defect as it was found ----

def test_a_pass_that_sleeps_past_the_deadline_in_its_retries_commits_nothing(tmp_path):
    """Spain's settings. The pass begins at 12:41, before the 13:00 gate. Every
    fetch fails; the two retry sleeps (5 and 15 minutes) pass; it continues on
    stored data, so the leak guard sees nothing. The write would be at 13:01.
    The guard used to judge 12:41 and four receipts were written."""
    m = _market(tmp_path)
    clock = Clock(_at(m, 12, 41))
    s = tick(market=m, fetch=_down, now_fn=clock, sleep=clock.sleep)
    assert clock.t == _at(m, 13, 1), "the scenario itself: 20 minutes of sleeps"
    assert s["committed"] == [] and _receipts(m) == []
    assert s["primary_receipt_stands"] is False
    assert m.receipts_path.read_bytes() == b"", (
        "the file was opened before the reading and nothing was written to it: "
        "it may remain, empty; it is never removed")
    refused = [msg for msg in s["skipped"] if "clock guard" in msg]
    assert [msg.split(":")[0] for msg in refused] == PANEL
    assert refused[0].startswith(f"{PANEL[0]}: past the 13:00 Europe/Madrid commit deadline "
                                 "at the moment of writing (2026-09-01 13:01:00; "
                                 "this pass began at 12:41:00)")
    for name, msg in zip(PANEL[1:], refused[1:]):
        assert msg.startswith(f"{name}: this pass already crossed the 13:00 Europe/Madrid "
                              "commit deadline at 2026-09-01 13:01:00")


# ---- 2. no retry is needed ----

def test_one_slow_fetch_across_the_gate_is_enough(tmp_path):
    """A market with no retries (every market but Spain). It begins ten seconds
    before its 10:00 gate and its one fetch takes twenty. The closest stamp
    in the record is 3.4 seconds before such a gate."""
    m = _market(tmp_path, tz="Asia/Tokyo", deadline=10, retries=(), slug="jp")
    clock = Clock(_at(m, 9, 59, 50))

    def slow_fetch(a, b):
        clock.sleep(20)
        return _history(DAY)                 # today's prices; tomorrow's not out

    s = tick(market=m, fetch=slow_fetch, now_fn=clock, sleep=clock.sleep)
    assert "fetch_error" not in s
    assert s["committed"] == [] and _receipts(m) == []
    assert sum("at the moment of writing (2026-09-01 10:00:10" in x for x in s["skipped"]) == 1
    assert sum("already crossed the 10:00 Asia/Tokyo commit deadline at 2026-09-01 10:00:10" in x
               for x in s["skipped"]) == len(PANEL) - 1


# ---- 3. an honest pass is unchanged, and its stamp is the reading judged ----

def test_a_pass_that_retries_and_still_writes_before_the_gate_commits_as_before(tmp_path):
    m = _market(tmp_path)
    clock = Clock(_at(m, 12, 30))
    s = tick(market=m, fetch=_down, now_fn=clock, sleep=clock.sleep)
    assert clock.t == _at(m, 12, 50)
    assert [r["strategy"] for r in s["committed"]] == PANEL
    assert not any("clock guard" in msg for msg in s["skipped"])
    assert s["primary_receipt_stands"] is True
    for r in _receipts(m):
        assert list(r) == RECEIPT_KEYS, "no field added, removed, renamed or reordered"
        assert r["committed_at"] == "2026-09-01T10:50:00+00:00", "12:50 Madrid, in UTC"
        assert STAMP.match(r["committed_at"])
        assert r["basis_day"] == "2026-09-01" and r["target"] == "2026-09-02"


def test_in_production_the_stamp_is_the_reading_taken_just_before_each_append(tmp_path, monkeypatch):
    """No clock injected: the tick uses `market_now`. It must be read once at
    the start and once per receipt, immediately before that receipt is written,
    and each receipt must carry exactly its own reading (in UTC, with
    microseconds, the form every receipt in the record has)."""
    m = _market(tmp_path)
    readings = []

    def fake_now(tz):
        t = datetime(2026, 9, 1, 11, 0, 0, 123456, tzinfo=tz) + timedelta(seconds=7 * len(readings))
        readings.append(t)
        return t

    monkeypatch.setattr(loop, "market_now", fake_now)
    s = tick(market=m, fetch=lambda a, b: _history(DAY))
    assert len(readings) == 1 + len(PANEL)
    stamps = [r["committed_at"] for r in _receipts(m)]
    assert stamps == [t.astimezone(timezone.utc).isoformat() for t in readings[1:]]
    assert stamps[0] == "2026-09-01T09:00:07.123456+00:00"
    assert all(STAMP.match(x) for x in stamps) and len(s["committed"]) == len(PANEL)


# ---- 4. crossing midnight ----

def test_a_pass_that_runs_into_the_next_day_commits_nothing(tmp_path):
    """Its `today` and its target were fixed at the start. By the time it
    writes, the target day has begun: 01:00 is before the 10:00 deadline by the
    hour, and still no receipt."""
    m = _market(tmp_path, deadline=10, retries=(16 * 3600,))
    clock = Clock(_at(m, 9, 0))
    s = tick(market=m, fetch=_down, now_fn=clock, sleep=clock.sleep)
    assert clock.t == _at(m, 1, 0, day=DAY + timedelta(days=1))
    assert s["target"] == "2026-09-02"
    assert s["committed"] == [] and _receipts(m) == []
    assert sum("2026-09-02 01:00:00" in x and "clock guard" in x for x in s["skipped"]) == len(PANEL)


# ---- 5. the boundary ----

@pytest.mark.parametrize("seconds_before_gate, commits", [(1, True), (0, False), (-1, False)])
def test_the_last_second_before_the_gate_commits_and_the_gate_itself_does_not(
        tmp_path, seconds_before_gate, commits):
    m = _market(tmp_path, retries=())
    clock = Clock(_at(m, 12, 0))
    gate = _at(m, 13, 0)

    def fetch(a, b):
        clock.t = gate - timedelta(seconds=seconds_before_gate)
        return _history(DAY)

    s = tick(market=m, fetch=fetch, now_fn=clock, sleep=clock.sleep)
    assert (len(s["committed"]) == len(PANEL)) is commits
    assert (len(_receipts(m)) == len(PANEL)) is commits
    if commits:
        assert _receipts(m)[0]["committed_at"] == "2026-09-01T10:59:59+00:00"


# ---- 6. for any start, any durations, any failures ----

@settings(max_examples=120, deadline=None)
@given(start=st.integers(0, 86399), deadline=st.integers(1, 23),
       retries=st.lists(st.integers(0, 4000), max_size=3),
       fetches=st.lists(st.tuples(st.integers(0, 4000), st.booleans()), min_size=4, max_size=4),
       between_writes=st.integers(0, 2000))
def test_no_receipt_is_ever_stamped_at_or_after_its_deadline(start, deadline, retries,
                                                             fetches, between_writes):
    """Whatever the start time, however long each fetch attempt takes, whether
    it fails, however long the sleeps, and however long passes between two
    strategies' writes: every receipt written is stamped on its basis day,
    before the deadline; and each stamp is a reading the tick took."""
    with tempfile.TemporaryDirectory() as tmp:
        m = _market(Path(tmp), deadline=deadline, retries=tuple(retries))
        begin = datetime(2026, 9, 1, tzinfo=m.tz) + timedelta(seconds=start)
        attempts = iter(fetches)

        class Drifting(Clock):
            def __call__(self):
                t = super().__call__()
                if len(self.reads) > 1:              # time also passes between writes
                    self.t += timedelta(seconds=between_writes)
                return t

        clock = Drifting(begin)

        def fetch(a, b):
            took, fails = next(attempts)
            clock.sleep(took)
            if fails:
                raise OSError("down")
            return _history(DAY)

        s = tick(market=m, fetch=fetch, now_fn=clock, sleep=clock.sleep)
        written = _receipts(m)
        assert len(written) == len(s["committed"])
        taken = {t.astimezone(timezone.utc).isoformat() for t in clock.reads}
        for r in written:
            local = datetime.fromisoformat(r["committed_at"]).astimezone(m.tz)
            assert local.date().isoformat() == r["basis_day"] == "2026-09-01"
            assert local.hour < deadline
            assert r["committed_at"] in taken
        if begin.hour >= deadline:
            assert written == [], "the start check still refuses by itself"


# ---- 7. stricter only ----

def test_with_a_clock_that_does_not_move_the_guard_decides_exactly_as_it_did(tmp_path):
    """The old condition is kept and the new one added. With no time passing
    between start and write the two readings are equal, so the outcome must be
    the old guard's outcome at every start time: the whole panel, or nothing.
    (Half-hour steps over two days, and both sides of the gate to the second.)"""
    starts = [datetime(2026, 9, 1, tzinfo=loop.MARKET_TZ) + timedelta(minutes=30 * i)
              for i in range(96)]
    starts += [datetime(2026, 9, 1, 12, 59, 59, tzinfo=loop.MARKET_TZ),
               datetime(2026, 9, 1, 13, 0, 0, tzinfo=loop.MARKET_TZ)]
    for i, begin in enumerate(starts):
        m = _market(tmp_path / str(i), retries=())
        s = tick(market=m, fetch=lambda a, b: _history(DAY), today=DAY, now_fn=lambda: begin)
        tomorrow = DAY + timedelta(days=1)
        old_guard_refuses = begin.date() >= tomorrow or (
            begin.date() == DAY and begin.hour >= m.deadline_hour)
        assert (s["committed"] == []) is old_guard_refuses, begin
        assert len(s["committed"]) in (0, len(PANEL)), begin


def test_a_clock_that_steps_back_cannot_reopen_the_window(tmp_path):
    """Why the start check is kept and not replaced. A pass that begins after
    the gate is refused there, whatever a later reading says: if the machine's
    clock were set back mid-pass, the write reading alone would let it through."""
    m = _market(tmp_path, retries=())
    readings = iter([_at(m, 13, 5)] + [_at(m, 12, 55)] * len(PANEL))
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: next(readings))
    assert s["committed"] == [] and _receipts(m) == []
    assert any("(13:05) — no receipts (clock guard)" in x for x in s["skipped"])


# ---- found in the review of this fix ----

def test_time_that_passes_while_the_file_is_prepared_counts(tmp_path, monkeypatch):
    """Before a line can be appended the folder is checked, a torn last line is
    repaired (with an fsync) and the file is opened. The reading used to be
    taken BEFORE that work: 12:59:59, two seconds of repair, a receipt written
    after 13:00 with a stamp before it, and the detector on the record would
    have passed, because it reads the stamp. The reading is taken after the
    preparation, immediately before the write."""
    m = _market(tmp_path, retries=())
    m.receipts_path.parent.mkdir(parents=True, exist_ok=True)
    m.receipts_path.write_text('{"torn": tr')              # an interrupted earlier write
    clock = Clock(_at(m, 12, 59, 59))
    real_repair = loop._repair_torn_tail

    def slow_repair(path):
        clock.sleep(2)                                      # the repair takes two seconds
        real_repair(path)

    monkeypatch.setattr(loop, "_repair_torn_tail", slow_repair)
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=clock, sleep=clock.sleep)
    assert (m.receipts_path.parent / "receipts.jsonl.corrupt").exists(), "the repair did run"
    assert s["committed"] == [] and m.receipts_path.read_text() == ""
    assert any("at the moment of writing (2026-09-01 13:00:01" in x for x in s["skipped"])


def test_a_reading_on_an_earlier_day_is_refused_too(tmp_path):
    """The pass begins at 00:00:05 on its day; the clock is then set back to
    23:59:58 of the day before. That is before the deadline by the hour and not
    on a later day, and four receipts were written with a basis day of today
    and a stamp of yesterday: receipts the detector on the record would then
    have turned main red for. At the write, any day but the pass's own is
    refused."""
    m = _market(tmp_path, retries=())
    eve = DAY - timedelta(days=1)
    readings = iter([_at(m, 0, 0, 5)] + [_at(m, 23, 59, 58, day=eve)] * len(PANEL))
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: next(readings))
    assert s["date"] == "2026-09-01"
    assert s["committed"] == [] and _receipts(m) == []
    assert any("at the moment of writing (2026-08-31 23:59:58" in x for x in s["skipped"])


def test_the_deadline_is_the_markets_whatever_zone_the_clock_answers_in(tmp_path):
    """A clock injected in UTC: 11:10Z is 13:10 in Madrid in September, past
    the gate, and four receipts were written because the guard compared the
    hour 11 with 13. Every reading is converted to the market's zone first.
    The same instant given in two zones must give the same outcome, the same
    day, and the same stamps."""
    utc = timezone.utc
    late_utc = datetime(2026, 9, 1, 11, 10, tzinfo=utc)
    m = _market(tmp_path / "late", retries=())
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: late_utc)
    assert s["committed"] == [] and any("deadline (13:10)" in x for x in s["skipped"])

    in_time_utc = datetime(2026, 9, 1, 8, 50, tzinfo=utc)
    stamps = []
    for name, reading in (("utc", in_time_utc), ("local", in_time_utc.astimezone(m.tz))):
        mk = _market(tmp_path / name, retries=())
        s = tick(market=mk, fetch=lambda a, b: _history(DAY), now_fn=lambda r=reading: r)
        assert [r["strategy"] for r in s["committed"]] == PANEL
        stamps.append([r["committed_at"] for r in _receipts(mk)])
    assert stamps[0] == stamps[1] == ["2026-09-01T08:50:00+00:00"] * len(PANEL)

    # the day itself comes from the market's zone: 22:30Z on 08-31 is 00:30 on 09-01 in Madrid
    mk = _market(tmp_path / "day", retries=())
    s = tick(market=mk, fetch=lambda a, b: _history(DAY),
             now_fn=lambda: datetime(2026, 8, 31, 22, 30, tzinfo=utc))
    assert s["date"] == "2026-09-01" and s["target"] == "2026-09-02"
    assert {r["basis_day"] for r in _receipts(mk)} == {"2026-09-01"}


def test_a_clock_reading_with_no_zone_is_refused(tmp_path):
    m = _market(tmp_path, retries=())
    with pytest.raises(ValueError, match="no zone"):
        tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: datetime(2026, 9, 1, 11, 0))
    assert _receipts(m) == []


def test_one_refusal_closes_the_pass_even_if_the_clock_then_steps_back(tmp_path):
    """Each receipt is judged at its own write, so after a refusal the next
    strategy used to read the clock again. A step back to 12:59 let the later
    strategies be written while an earlier one had been refused. The first
    refusal now ends the pass: the clock is not even read again."""
    m = _market(tmp_path, retries=())
    given = []

    def now():
        seq = [_at(m, 12, 30), _at(m, 12, 59, 59), _at(m, 13, 0, 0),
               _at(m, 12, 59, 0), _at(m, 12, 59, 1)]
        given.append(seq[len(given)])
        return given[-1]

    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=now)
    assert [r["strategy"] for r in s["committed"]] == PANEL[:1]
    assert [r["strategy"] for r in _receipts(m)] == PANEL[:1]
    assert len(given) == 3, "start, the first write, the refused write: and no reading after that"
    refused = [x for x in s["skipped"] if "clock guard" in x]
    assert [x.split(":")[0] for x in refused] == PANEL[1:]
    assert all("2026-09-01 13:00:00" in x for x in refused)


def test_a_receipt_of_another_version_does_not_stand_in_for_this_one(tmp_path, monkeypatch):
    """The idempotency check keyed on (target, strategy). Receipts, settlement
    and the invariants key on (target, strategy, strategy_version). A version-1
    receipt would have suppressed an on-time version-2 receipt of the same
    name. And the heartbeat's question, whether the PRIMARY receipt stands, is
    about the primary's own version."""
    m = _market(tmp_path, retries=())
    v1 = loop.STRATEGIES[0]
    v2 = {**v1, "strategy_version": "2"}
    at = lambda: _at(m, 11, 0)                                       # noqa: E731
    monkeypatch.setattr(loop, "STRATEGIES", [v1])
    first = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=at)
    assert [(r["strategy"], r["strategy_version"]) for r in first["committed"]] == [(v1["strategy"], "1")]
    monkeypatch.setattr(loop, "STRATEGIES", [v1, v2])
    second = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=at)
    assert [(r["strategy"], r["strategy_version"]) for r in second["committed"]] == [(v1["strategy"], "2")]
    assert sum("already committed (idempotent)" in x for x in second["skipped"]) == 1
    third = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=at)
    assert third["committed"] == [] and sum("idempotent" in x for x in third["skipped"]) == 2

    only_v2 = _market(tmp_path / "only-v2", retries=())
    monkeypatch.setattr(loop, "STRATEGIES", [v2])
    s = tick(market=only_v2, fetch=lambda a, b: _history(DAY), now_fn=at)
    assert len(s["committed"]) == 1 and s["primary_receipt_stands"] is False


def test_a_receipt_line_without_a_version_stops_the_tick_as_it_always_did(tmp_path):
    """Every receipt in the record carries `strategy_version`. A line without
    it is not guessed at: settlement indexes the field and the tick stops with
    an error before the commit step, on main and here."""
    m = _market(tmp_path, retries=())
    m.receipts_path.parent.mkdir(parents=True, exist_ok=True)
    m.receipts_path.write_text(json.dumps({"target": "2026-09-02", "strategy": PANEL[0]}) + "\n")
    with pytest.raises(KeyError, match="strategy_version"):
        tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: _at(m, 11, 0))


class WritePath:
    """Slows down the two things that used to sit between the clock reading and
    the bytes reaching the file (building the JSON text, and the write itself
    going through a buffer), and records WHEN, on the test's clock, each line
    was handed to the operating system."""
    def __init__(self, clock, monkeypatch, *, dumps_takes=2):
        self.clock, self.writes = clock, []
        real_dumps, real_write = json.dumps, loop.os.write

        def slow_dumps(*a, **k):
            clock.sleep(dumps_takes)                 # encoding takes two seconds
            return real_dumps(*a, **k)

        def write(fd, data):
            data = bytes(data)
            if b'"committed_at"' in data:
                self.writes.append((clock.t, data))
            return real_write(fd, data)

        monkeypatch.setattr(loop.json, "dumps", slow_dumps)
        monkeypatch.setattr(loop.os, "write", write)

    def lines(self) -> list[tuple[datetime, dict]]:
        return [(at, json.loads(data)) for at, data in self.writes]


def test_nothing_slow_stands_between_the_reading_and_the_write(tmp_path, monkeypatch):
    """Found in the second review of this fix: after the judged reading, the
    receipt's dictionary was still built, rounded, turned into JSON and pushed
    through a buffered file object. With that work taking two seconds, a
    reading of 12:59:59 became a line stamped 12:59:59 and written at 13:00:01.

    Now the whole line is encoded BEFORE the reading, with a slot for the
    stamp, and after the reading there is one write. Measured on a clock that
    only the slow steps advance: every line is handed to the operating system
    at the very reading it is stamped with, in one call, complete. And the
    receipt that would have crossed is refused, not written late."""
    m = _market(tmp_path, retries=())
    clock = Clock(_at(m, 12, 59, 53))
    path = WritePath(clock, monkeypatch)
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=clock, sleep=clock.sleep)
    written = path.lines()
    assert 1 <= len(written) < len(PANEL), (
        "the scenario: every encoding takes 2 s, so some receipts are written "
        "before 13:00 and the pass then crosses it")
    for handed_over, rec in written:
        stamp = datetime.fromisoformat(rec["committed_at"])
        assert stamp == handed_over, (
            f"stamped {stamp:%H:%M:%S} but handed to the operating system at "
            f"{handed_over:%H:%M:%S}: something slow ran after the reading")
        assert handed_over.astimezone(m.tz).hour < m.deadline_hour
    assert all(data.endswith(b"\n") and data.count(b"\n") == 1 for _, data in path.writes), \
        "each receipt is one complete line in one write"
    assert [r["strategy"] for r in s["committed"]] == [rec["strategy"] for _, rec in written]
    assert [r["strategy"] for r in _receipts(m)] == PANEL[:len(written)]
    assert any("at the moment of writing (2026-09-01 13:00:" in x for x in s["skipped"])


def test_the_line_written_is_byte_for_byte_the_json_of_the_receipt(tmp_path):
    """Encoding around a slot must not change what is on disk: each line is
    exactly `json.dumps(receipt)` and a newline, the receipt being the one the
    summary returns, with its stamp."""
    m = _market(tmp_path, retries=())
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: _at(m, 11, 0, 7))
    assert len(s["committed"]) == len(PANEL)
    assert m.receipts_path.read_bytes() == b"".join(
        (json.dumps(r) + "\n").encode() for r in s["committed"])
    assert all(list(r) == RECEIPT_KEYS and STAMP.match(r["committed_at"]) for r in s["committed"])
    assert "\\u0000" not in m.receipts_path.read_text(), "no trace of the slot"


def test_the_stamp_slot_must_appear_exactly_once():
    ok = {"a": 1, "committed_at": loop._STAMP_SLOT}
    head, tail = loop._encode_around_stamp(ok)
    assert head + b"X" + tail == b'{"a": 1, "committed_at": "X"}\n'
    for bad in ({"a": 1}, {"a": loop._STAMP_SLOT, "committed_at": loop._STAMP_SLOT}):
        with pytest.raises(ValueError, match="exactly once"):
            loop._encode_around_stamp(bad)


def test_a_short_write_is_completed(tmp_path, monkeypatch):
    """`os.write` may take fewer bytes than it was given. The rest follows; the
    line on disk is whole."""
    m = _market(tmp_path, retries=())
    real_write = loop.os.write
    monkeypatch.setattr(loop.os, "write", lambda fd, data: real_write(fd, bytes(data)[:7]))
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: _at(m, 11, 0))
    assert [json.loads(l)["strategy"] for l in m.receipts_path.read_text().splitlines()] == PANEL
    assert len(s["committed"]) == len(PANEL)


def test_a_pass_refused_at_the_write_still_repairs_a_torn_tail_and_keeps_its_bytes(tmp_path):
    """The file is prepared before the reading, so a pass that is then refused
    has already repaired a torn last line: the partial bytes are moved to
    `.corrupt`, never dropped, and the receipts file is left ending cleanly."""
    m = _market(tmp_path, retries=())
    eve = DAY - timedelta(days=1)
    loop.save_prices(_history(eve), m.prices_path)
    tick(market=m, fetch=lambda a, b: _history(eve), now_fn=lambda: _at(m, 11, 0, day=eve))
    whole = m.receipts_path.read_text()
    assert whole.count("\n") == len(PANEL)
    with m.receipts_path.open("a") as fh:
        fh.write('{"torn": tr')                             # an interrupted later write
    readings = iter([_at(m, 12, 59, 59), _at(m, 13, 0, 0)])
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: next(readings))
    assert s["committed"] == []
    assert m.receipts_path.read_text() == whole
    assert (m.receipts_path.parent / "receipts.jsonl.corrupt").read_text() == '{"torn": tr\n'


class Descriptors:
    """Records, for the receipts file only, each open, write, fsync and close
    of a file descriptor, in order."""
    def __init__(self, monkeypatch):
        self.events, self.mine = [], set()
        o, w, s, c = loop.os.open, loop.os.write, loop.os.fsync, loop.os.close

        def opened(path, flags, mode=0o777, **kw):
            fd = o(path, flags, mode, **kw)
            if str(path).endswith("receipts.jsonl"):
                self.mine.add(fd)
                self.events.append("open")
            return fd

        def wrote(fd, data):
            if fd in self.mine:
                self.events.append("write")
            return w(fd, data)

        def synced(fd):
            if fd in self.mine:
                self.events.append("fsync")
            return s(fd)

        def closed(fd):
            if fd in self.mine:
                self.mine.discard(fd)
                self.events.append("close")
            return c(fd)

        for name, fn in (("open", opened), ("write", wrote), ("fsync", synced), ("close", closed)):
            monkeypatch.setattr(loop.os, name, fn)


def test_each_receipt_is_synced_to_disk_and_its_descriptor_closed(tmp_path, monkeypatch):
    """Two things nothing pinned, here or before this change. A receipt must be
    fsynced after it is written and before the tick reports it committed:
    without that, a crash can lose a receipt the summary has already counted.
    And the descriptor must be closed on every path."""
    m = _market(tmp_path, retries=())
    seen = Descriptors(monkeypatch)
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: _at(m, 11, 0))
    assert len(s["committed"]) == len(PANEL)
    assert seen.events == ["open", "write", "fsync", "close"] * len(PANEL)
    assert seen.mine == set(), "no descriptor left open"


def test_a_refused_receipt_leaves_no_descriptor_open(tmp_path, monkeypatch):
    """The file is opened before the reading, so a refusal happens with a
    descriptor in hand. It must be closed there too, and nothing written."""
    m = _market(tmp_path, retries=())
    seen = Descriptors(monkeypatch)
    readings = iter([_at(m, 12, 59, 59), _at(m, 13, 0, 0)])
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: next(readings))
    assert s["committed"] == []
    assert seen.events == ["open", "close"], "opened once, refused, closed; the pass then ends"
    assert seen.mine == set(), "the descriptor of the refused receipt was left open"


def test_a_write_that_fails_still_closes_the_descriptor(tmp_path, monkeypatch):
    m = _market(tmp_path, retries=())
    seen = Descriptors(monkeypatch)
    patched_write = loop.os.write

    def failing(fd, data):
        if fd in seen.mine:
            patched_write(fd, data)
            raise OSError("disk full")
        return patched_write(fd, data)

    monkeypatch.setattr(loop.os, "write", failing)
    with pytest.raises(OSError, match="disk full"):
        tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: _at(m, 11, 0))
    assert seen.events == ["open", "write", "close"] and seen.mine == set()


# ---- judged per receipt: a pass can cross the gate between two writes ----

_SPEC = importlib.util.spec_from_file_location(
    "compare_strategies", ROOT / "scripts" / "compare_strategies.py")
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)


def test_a_pass_that_crosses_the_gate_between_two_writes_leaves_a_partial_panel(tmp_path):
    """Each receipt is judged at its own write. So a pass can write the first
    two strategies' receipts at 12:59:58 and 12:59:59 and be refused the third
    at 13:00:00, which closes the pass for the fourth. The summary names each refused strategy with
    the reading that closed the pass. The day then settles for the two that have a receipt and for
    no other, and the comparison script counts it only for pairs that both
    have a row: all of which is keyed on (target, strategy)."""
    m = _market(tmp_path, retries=())
    # a first, ordinary day: the whole panel commits for target 09-01
    eve = DAY - timedelta(days=1)
    loop.save_prices(_history(eve), m.prices_path)       # 09-01 not published yet
    first = tick(market=m, fetch=lambda a, b: _history(eve), now_fn=lambda: _at(m, 11, 0, day=eve))
    assert [r["strategy"] for r in first["committed"]] == PANEL
    readings = iter([_at(m, 12, 30), _at(m, 12, 59, 58), _at(m, 12, 59, 59),
                     _at(m, 13, 0, 0), _at(m, 13, 0, 1)])
    s = tick(market=m, fetch=lambda a, b: _history(DAY), now_fn=lambda: next(readings))
    assert [r["strategy"] for r in s["committed"]] == PANEL[:2]
    refused = [x for x in s["skipped"] if "clock guard" in x]
    assert [x.split(":")[0] for x in refused] == PANEL[2:]
    assert "at the moment of writing (2026-09-01 13:00:00" in refused[0]
    assert "already crossed the 13:00 Europe/Madrid commit deadline at 2026-09-01 13:00:00" in refused[1]
    assert s["primary_receipt_stands"] is True
    assert sorted((e["target"], e["strategy"]) for e in s["settled"]) == sorted(
        ("2026-09-01", n) for n in PANEL), "the first day settled whole, in the same pass"
    on_day = [r for r in _receipts(m) if r["target"] == "2026-09-02"]
    assert [r["strategy"] for r in on_day] == PANEL[:2]
    assert [r["committed_at"] for r in on_day] == ["2026-09-01T10:59:58+00:00",
                                                   "2026-09-01T10:59:59+00:00"]

    # the partial day settles once its prices are in: for the two with a receipt only
    later = DAY + timedelta(days=2)
    done = tick(market=m, fetch=lambda a, b: _history(later), now_fn=lambda: _at(m, 17, 0, day=later))
    assert sorted((e["target"], e["strategy"]) for e in done["settled"]) == sorted(
        ("2026-09-02", n) for n in PANEL[:2])
    again = tick(market=m, fetch=lambda a, b: _history(later), now_fn=lambda: _at(m, 17, 5, day=later))
    assert again["settled"] == [], "the refused strategies never settle for that target"

    rows = [json.loads(l) for l in m.ledger_path.read_text().splitlines()]
    assert len(rows) == len(PANEL) + 2
    both_have_it = cs.pair(rows, PANEL[0], PANEL[1])
    one_lacks_it = cs.pair(rows, PANEL[0], PANEL[3])
    assert both_have_it["shared"] == 2
    assert one_lacks_it["shared"] == 1, "the partial day is not a shared day for this pair"
