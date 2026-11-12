"""The anchoring-lag measurement (register D-07; red-team WEAK 3a, 2026-10-01).

VERIFY.md's strong check rests on the time of the Bitcoin block a proof ends in.
Nobody had measured how long after the stamp that block is mined, so nobody knew
whether it fell before or after the publication the receipt must predate. These
tests pin the measurement: which receipts a manifest covers, which block anchors
each receipt, the reference instant per market, and that a late anchor which is
not disclosed fails the run while an unreachable explorer is INCONCLUSIVE."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "ots_block_lag", ROOT / "scripts" / "ots_block_lag.py")
lag = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lag)

UTC = timezone.utc


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _proof(*heights: int) -> bytes:
    """A stand-in .ots file: junk, then one Bitcoin attestation per height."""
    body = b"\x00OpenTimestamps\x00\x00Proof\x00" + bytes(range(40))
    for h in heights:
        payload = _leb(h)
        body += b"\x08\xf1\x04abcd\x00" + lag.BITCOIN_TAG + _leb(len(payload)) + payload
    return body


# --- reading a proof ---

def test_block_heights_are_read_from_the_proof_bytes():
    assert lag.block_heights(_proof(971559, 971544)) == [971544, 971559]
    assert lag.block_heights(_proof()) == [], "a pending proof attests no block"
    assert lag.block_heights(_proof(100) + lag.BITCOIN_TAG) == [100], "truncated tail ignored"


def test_block_heights_agree_with_the_ots_client_on_a_real_proof():
    """`ots info` on this proof printed BitcoinBlockHeaderAttestation heights
    971544, 971545, 971559 and 971585 (checked 2026-11-11). A stamped manifest
    and its upgraded proof are immutable, so the answer cannot drift."""
    proof = ROOT / "Data" / "es" / "ots" / "2026-10-15.txt.ots"
    if not proof.exists():
        return
    assert lag.block_heights(proof.read_bytes()) == [971544, 971545, 971559, 971585]


# --- the reference instant ---

def test_reference_instants_follow_each_market_and_its_clock_changes():
    ref = lag.reference_instant
    assert ref("es", "2026-10-11") == datetime(2026, 10, 10, 11, 15, tzinfo=UTC)   # CEST
    assert ref("es", "2026-11-03") == datetime(2026, 11, 2, 12, 15, tzinfo=UTC)    # CET
    assert ref("de", "2026-10-11") == datetime(2026, 10, 10, 10, 40, tzinfo=UTC)
    assert ref("ercot", "2026-11-03") == datetime(2026, 11, 2, 19, 30, tzinfo=UTC)  # CST
    # GB is not an auction: the first price for T is the first half hour of T.
    assert ref("gb", "2026-09-01") == datetime(2026, 8, 31, 23, 0, tzinfo=UTC)     # BST
    assert ref("gb", "2026-11-03") == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


# --- which block anchors which receipt ---

def _receipt(target: str, committed: str, strategy: str = "p") -> str:
    return json.dumps({"target": target, "strategy": strategy,
                       "committed_at": committed}) + "\n"


def _market(tmp_path: Path, lines: list[str], manifests: dict[str, tuple[int, tuple]],
            slug: str = "es") -> Path:
    """manifests: name -> (receipt lines covered, attested heights or () = pending;
    None instead of the tuple = written but never stamped)."""
    d = tmp_path / slug
    (d / "ots").mkdir(parents=True)
    (d / "receipts.jsonl").write_text("".join(lines))
    for name, (covered, heights) in manifests.items():
        digest = hashlib.sha256("".join(lines[:covered]).encode()).hexdigest()
        (d / "ots" / name).write_text(
            f"esios-paper audit manifest x\nsha256(receipts.jsonl)={digest}\n"
            "sha256(ledger.jsonl)=absent\n")
        if heights is not None:
            (d / "ots" / f"{name}.ots").write_bytes(_proof(*heights))
    return d


LINES = [_receipt("2026-10-11", "2026-10-10T09:00:30+00:00", "p"),
         _receipt("2026-10-11", "2026-10-10T09:00:31+00:00", "c"),
         _receipt("2026-10-12", "2026-10-11T09:00:30+00:00", "p"),
         _receipt("2026-10-13", "2026-10-12T09:00:30+00:00", "p")]
BLOCKS = {100: datetime(2026, 10, 10, 9, 20, tzinfo=UTC),     # 115 min before 11:15Z
          205: datetime(2026, 10, 11, 10, 45, tzinfo=UTC),    # 30 min before
          210: datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
          300: datetime(2026, 10, 12, 9, 5, tzinfo=UTC)}


def test_each_receipt_is_anchored_by_the_earliest_block_that_covers_it(tmp_path):
    d = _market(tmp_path, LINES, {
        "2026-10-10.txt": (2, (100,)),
        "2026-10-11.txt": (3, (210, 205)),        # two calendars: the earlier block counts
        "2026-10-11-2.txt": (3, None),            # written, never stamped: ignored
        "2026-10-12.txt": (4, ()),                # still pending
        "2026-10-12-2.txt": (3, (300,)),          # later block, covers lines 1-3 only
    })
    m = lag.measure(d, "es", BLOCKS.__getitem__)
    rows = m.rows
    assert [(r.target, r.height) for r in rows] == [("2026-10-11", 100), ("2026-10-12", 205)]
    assert m.unanchored == 1 and m.pending == 1, "the 10-13 receipt has no Bitcoin anchor yet"
    assert m.pre_stamping == 0 and str(m.first_stamp) == "2026-10-10"
    assert rows[0].lag_min == pytest.approx(19.5) and rows[0].margin_min == pytest.approx(115)
    assert rows[1].margin_min == pytest.approx(30)
    s = lag.summarise(rows)
    assert (s["targets"], s["late"], s["under_1h"], s["tightest"]) == (2, 0, 1, 30)
    assert lag.late(rows) == [] and lag.undisclosed("es", rows) == []


def test_a_target_day_is_judged_on_its_last_anchored_receipt(tmp_path):
    """Two receipts for one target, anchored by different blocks: the day is
    only as good as the later one."""
    d = _market(tmp_path, LINES[:2], {"a.txt": (1, (100,)), "b.txt": (2, (210,))})
    rows = lag.measure(d, "es", BLOCKS.__getitem__).rows
    assert [(r.target, r.height) for r in rows] == [("2026-10-11", 210)]
    assert lag.late(rows) == rows, "block 12:00Z on 10-11 is after 11:15Z on 10-10"


def test_a_manifest_that_matches_no_prefix_anchors_nothing(tmp_path):
    d = _market(tmp_path, LINES[:1], {"a.txt": (1, (100,))})
    (d / "ots" / "b.txt").write_text("sha256(receipts.jsonl)=" + "0" * 64 + "\n")
    (d / "ots" / "b.txt.ots").write_bytes(_proof(1))
    (d / "ots" / "c.txt").write_text("no hash line here\n")
    (d / "ots" / "c.txt.ots").write_bytes(_proof(2))
    receipts, earliest, pending = lag.coverage(d)
    assert len(receipts) == 1 and earliest == {1: 100} and pending == 0


def test_receipts_committed_before_stamping_began_are_counted_apart(tmp_path):
    """ES committed receipts from 2026-07-30 but its first manifest is dated
    2026-08-08: the earlier receipts are covered only by that manifest, whose
    block is days after their prices published. That tier is disclosed and
    cannot be repaired; it must be counted, not reported as nine late anchors —
    and not hidden either."""
    lines = [_receipt("2026-10-09", "2026-10-08T09:00:30+00:00"),      # before any manifest
             _receipt("2026-10-10", "2026-10-09T09:00:30+00:00"),
             _receipt("2026-10-11", "2026-10-10T09:00:30+00:00")]
    d = _market(tmp_path, lines, {"2026-10-10.txt": (3, (100,)),
                                  "notes.txt": (3, None)})           # not a dated manifest
    m = lag.measure(d, "es", BLOCKS.__getitem__)
    assert str(m.first_stamp) == "2026-10-10" and m.pre_stamping == 2
    assert [r.target for r in m.rows] == ["2026-10-11"] and lag.late(m.rows) == []
    assert lag.first_stamp_day(tmp_path) is None


def test_a_late_anchor_is_a_finding_unless_it_is_a_disclosed_date(tmp_path):
    late_block = {7: datetime(2026, 10, 10, 10, 47, tzinfo=UTC)}     # 7 min after 12:40 Berlin
    d = _market(tmp_path, LINES[:1], {"a.txt": (1, (7,))}, slug="de")
    rows = lag.measure(d, "de", late_block.__getitem__).rows
    assert rows[0].margin_min == pytest.approx(-7)
    assert ("de", "2026-10-11") in lag.DISCLOSED_LATE
    assert lag.late(rows) == rows and lag.undisclosed("de", rows) == []
    # the same late day in a market where it is NOT disclosed is a finding
    assert lag.undisclosed("ercot", rows) == rows
    assert lag.summarise(rows)["tightest"] is None


def test_disclosed_late_set_is_exactly_the_three_dates_in_verify_md():
    assert lag.DISCLOSED_LATE == frozenset({
        ("es", "2026-08-09"), ("de", "2026-08-28"), ("de", "2026-10-11")})
    verify = " ".join((ROOT / "VERIFY.md").read_text().split())
    for needle in ("2026-08-09", "2026-08-28", "2026-10-11", "scripts/ots_block_lag.py"):
        assert needle in verify, f"VERIFY.md must disclose {needle}"


# --- the explorer ---

class _Resp:
    def __init__(self, text): self.text = text
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.text.encode()


def _http(url, code):
    return urllib.error.HTTPError(url, code, "x", {}, None)


def test_explorer_waits_out_a_429_then_reads_the_block_time_once():
    calls, sleeps = [], []
    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise _http(req.full_url, 429)
        if "/block-height/" in req.full_url:
            return _Resp("abc123\n")
        return _Resp('{"timestamp": 1760087220}')
    ex = lag.Explorer(("https://one.test/api/",), pause=0.2, waits=(5, 20),
                      _open=fake_open, _sleep=sleeps.append)
    assert ex.block_time(42) == datetime.fromtimestamp(1760087220, UTC)
    assert ex.block_time(42) == datetime.fromtimestamp(1760087220, UTC)
    assert calls == ["https://one.test/api/block-height/42",
                     "https://one.test/api/block-height/42",
                     "https://one.test/api/block/abc123"], "second lookup comes from the cache"
    assert sleeps == [0.2, 5, 0.2]


def test_explorer_moves_to_the_second_source_and_fails_loudly_when_both_refuse():
    calls = []
    def flaky(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.startswith("https://one.test"):
            raise _http(req.full_url, 503)            # not a rate limit: no retry there
        return _Resp("h" if "/block-height/" in req.full_url else '{"timestamp": 60}')
    ex = lag.Explorer(("https://one.test", "https://two.test"), _open=flaky,
                      _sleep=lambda s: None)
    assert ex.block_time(1) == datetime.fromtimestamp(60, UTC)
    assert calls[:2] == ["https://one.test/block-height/1", "https://two.test/block-height/1"]
    assert calls[2] == "https://two.test/block/h", "the source that answered stays in use"
    assert ex.block_time(2) and not any(c.startswith("https://one.test") for c in calls[2:])

    def down(req, timeout=None):
        raise OSError("no route")
    with pytest.raises(OSError):
        lag.Explorer(("https://one.test", "https://two.test"), _open=down,
                     _sleep=lambda s: None).block_time(1)

    def limited(req, timeout=None):
        raise _http(req.full_url, 429)
    with pytest.raises(urllib.error.HTTPError):
        lag.Explorer(("https://one.test",), waits=(1,), _open=limited,
                     _sleep=lambda s: None).block_time(1)


# --- the entry point ---

class _FakeExplorer:
    def __init__(self, blocks=None, fail=False):
        self.blocks, self.fail = blocks or BLOCKS, fail
    def block_time(self, height):
        if self.fail:
            raise OSError("explorer unreachable")
        return self.blocks[height]


def test_main_reports_clean_markets_and_exits_zero(tmp_path, monkeypatch, capsys):
    _market(tmp_path, LINES, {"2026-10-10.txt": (2, (100,)), "2026-10-11.txt": (3, (205,))})
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main(["--rows"], explorer=_FakeExplorer()) == 0
    out = capsys.readouterr().out
    assert "es: 2 target days 2026-10-11 to 2026-10-12" in out
    assert "block at or after the reference instant: 0 day(s)" in out
    assert "under one hour before it: 1; tightest margin 30 min" in out
    assert "not yet anchored: 1 receipt(s)" in out
    assert "target 2026-10-12" in out and "no undisclosed late anchor" in out
    assert "before stamping began" not in out


def test_main_reports_the_pre_stamping_tier(tmp_path, monkeypatch, capsys):
    lines = [_receipt("2026-10-10", "2026-10-09T09:00:30+00:00")] + LINES[:2]
    _market(tmp_path, lines, {"2026-10-10.txt": (3, (100,))})
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main([], explorer=_FakeExplorer()) == 0
    assert "committed before stamping began (2026-10-10): 1 receipt(s)" in capsys.readouterr().out


def test_main_fails_on_an_undisclosed_late_anchor(tmp_path, monkeypatch, capsys):
    _market(tmp_path, LINES[:2], {"a.txt": (2, (210,))})
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main([], explorer=_FakeExplorer()) == 1
    out = capsys.readouterr().out
    assert "LATE — NOT DISCLOSED" in out and "1 receipt day(s) anchored at or after" in out


def test_main_names_a_disclosed_late_day_without_failing(tmp_path, monkeypatch, capsys):
    late_block = {7: datetime(2026, 10, 10, 10, 47, tzinfo=UTC)}
    _market(tmp_path, LINES[:1], {"a.txt": (1, (7,))}, slug="de")
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main(["--market", "de"], explorer=_FakeExplorer(late_block)) == 0
    out = capsys.readouterr().out
    assert "LATE (disclosed)" in out and "tightest margin n/a" in out


def test_main_is_inconclusive_when_the_explorer_cannot_be_reached(tmp_path, monkeypatch, capsys):
    _market(tmp_path, LINES, {"a.txt": (2, (100,))})
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main([], explorer=_FakeExplorer(fail=True)) == 2
    assert "INCONCLUSIVE for es" in capsys.readouterr().out


def test_main_with_nothing_to_measure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lag, "DATA_DIR", tmp_path)
    assert lag.main([], explorer=_FakeExplorer()) == 0
    assert "nothing to measure" in capsys.readouterr().out
    _market(tmp_path, LINES[:1], {"a.txt": (1, ())})            # one pending proof only
    assert lag.main([], explorer=_FakeExplorer()) == 0
    assert "es: no receipt has a Bitcoin anchor yet" in capsys.readouterr().out
