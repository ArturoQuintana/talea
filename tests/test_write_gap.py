"""The read-to-write gap, observe-only (register T-35, D-36).

D-36 chose no safety margin before a market's deadline; instead, the time from
the clock reading that stamps a receipt to the write of its line is observed.
It is read outside loop.py, from the receipts file's own modification time,
which the operating system sets at the write: file time minus the last
receipt's stamp, in integer nanoseconds, plus the file clock's resolution,
rounded UP to whole milliseconds. That is an upper bound only under two stated
assumptions (the file clock lags by at most 10 ms; the clock did not step),
and every printed number says so. It is printed after each pass (server
journal, runner log) and carried in the BODY of the server's Data commit,
never in its subject. And it never stops a tick or a backup: any failure to
observe prints "not measured" and everything else goes on."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import talea.__main__ as cli

STAMP = datetime(2026, 11, 16, 10, 0, 0, 123456, tzinfo=timezone.utc)
SAYS = "(assumes the file clock lags ≤10 ms and the clock did not step)"


def _receipts(path: Path, *stamps: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"target": "2026-11-17", "committed_at": s.isoformat()}) + "\n"
                            for s in stamps))
    return path


def _file_time_ns(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def test_the_gap_is_the_file_time_minus_the_last_receipts_stamp_in_integer_ns(tmp_path):
    f = _receipts(tmp_path / "receipts.jsonl", STAMP - timedelta(seconds=30), STAMP)
    _file_time_ns(f, cli._ns(STAMP) + 2_500_000)
    assert cli.last_write_gap(f) == 2_500_000                     # the LAST receipt, exactly
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤13 ms"
    _file_time_ns(f, cli._ns(STAMP) + 250_500_000)
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤261 ms"


def test_rounding_is_up_and_exact_at_the_millisecond_boundary(tmp_path):
    """Codex, round 1: through floats, a stamp at S+0.125 s and a file time at
    S+0.126000040 s read as just under 1 ms and printed 11. In integer
    nanoseconds the gap is 1,000,040 ns, so the bound is 11,000,040 ns: 12 ms.
    And exactly on a boundary nothing is added: 2,000,000 ns prints 12."""
    s = datetime(2026, 11, 16, 10, 0, 0, 125000, tzinfo=timezone.utc)
    f = _receipts(tmp_path / "receipts.jsonl", s)
    _file_time_ns(f, cli._ns(s) + 1_000_040)
    assert cli.last_write_gap(f) == 1_000_040
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤12 ms"
    _file_time_ns(f, cli._ns(s) + 2_000_000)
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤12 ms"
    _file_time_ns(f, cli._ns(s) + 2_000_001)
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤13 ms"
    assert cli._ns(datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=timezone.utc)) == 1_000_001_000


def test_a_file_time_just_before_the_stamp_is_within_the_resolution_never_negative(tmp_path):
    """File times can lag the clock by the kernel's coarse tick: a file time a
    few ms BEFORE the stamp is a gap under the resolution."""
    f = _receipts(tmp_path / "receipts.jsonl", STAMP)
    _file_time_ns(f, cli._ns(STAMP) - 3_000_000)
    said = cli.describe_write_gap(cli.last_write_gap(f))
    assert said == "≤10 ms" and "-" not in said
    _file_time_ns(f, cli._ns(STAMP) - 9_999_999)
    assert cli.describe_write_gap(cli.last_write_gap(f)) == "≤10 ms"


def test_a_file_time_well_before_the_stamp_says_the_clock_stepped(tmp_path):
    f = _receipts(tmp_path / "receipts.jsonl", STAMP)
    for back in (10_000_000, 1_000_000_000):
        _file_time_ns(f, cli._ns(STAMP) - back)
        said = cli.describe_write_gap(cli.last_write_gap(f))
        assert said.startswith("not measured: the file's time is before the stamp")
        assert not re.search(r"-\d", said)


def test_nothing_to_measure_is_said_as_not_measured(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    torn = tmp_path / "torn.jsonl"
    torn.write_text('{"target": "2026-11-17", "committed_')
    unstamped = tmp_path / "unstamped.jsonl"
    unstamped.write_text('{"target": "2026-11-17"}\n')
    naive = tmp_path / "naive.jsonl"
    naive.write_text('{"committed_at": "2026-11-16T10:00:00"}\n')         # no zone: no instant
    for f in (empty, torn, unstamped, naive, tmp_path / "missing.jsonl"):
        assert cli.last_write_gap(f) is None
    assert cli.describe_write_gap(None) == "not measured"


def test_a_real_append_sets_the_file_time_and_the_reading_never_exceeds_the_elapsed_time(tmp_path):
    """Not simulated: a stamp read from the clock, then one write, as loop.py
    does, then the clock read again. The reading (file time minus stamp) can
    never exceed the time elapsed from the stamp to after the write: an
    independent oracle. If the operating system did not set the file time at
    the write, the reading would be from 2020, and this fails."""
    f = tmp_path / "receipts.jsonl"
    f.write_text(json.dumps({"committed_at": "2020-01-01T00:00:00+00:00"}) + "\n")
    _file_time_ns(f, cli._ns(datetime(2020, 1, 1, tzinfo=timezone.utc)))
    fd = os.open(f, os.O_WRONLY | os.O_APPEND)
    try:
        stamp = datetime.now(timezone.utc)
        os.write(fd, (json.dumps({"committed_at": stamp.isoformat()}) + "\n").encode())
    finally:
        os.close(fd)
    after = time.time_ns()
    gap = cli.last_write_gap(f)
    assert -cli.WRITE_GAP_RESOLUTION_NS < gap <= after - cli._ns(stamp), (gap, after - cli._ns(stamp))
    assert re.fullmatch(r"≤\d+ ms", cli.describe_write_gap(gap))


def _a_pass(monkeypatch, committed, gap=4_200_000):
    monkeypatch.setattr(cli, "tick", lambda **kw: {
        "market": "de", "date": "2026-11-16", "target": "2026-11-17", "settled": [],
        "committed": committed, "skipped": [], "primary_receipt_stands": True})
    calls = []
    for name in ("ots_stamp", "git_backup", "heartbeat", "email_digest"):
        monkeypatch.setattr(cli, name, lambda *a, n=name, **k: calls.append(n))
    read = []
    def measure(p):
        read.append(p)
        if isinstance(gap, Exception):
            raise gap
        return gap
    monkeypatch.setattr(cli, "last_write_gap", measure)
    return read, calls


COMMITTED = [{"target": "2026-11-17", "strategy": "x", "strategy_version": "1",
              "buy_hours": [3, 4], "sell_hours": [19, 20], "basis_day": "2026-11-16"}]


def test_each_pass_that_committed_prints_its_gap_with_its_assumptions(monkeypatch, capsys):
    read, _ = _a_pass(monkeypatch, COMMITTED)
    assert cli.cmd_tick("de") == 0
    assert f"[esios-paper:de] write gap, last receipt: ≤15 ms {SAYS}" in capsys.readouterr().out
    assert read[-1].parts[-2:] == ("de", "receipts.jsonl")
    assert cli.cmd_tick() == 0
    assert f"[esios-paper:es] write gap, last receipt: ≤15 ms {SAYS}" in capsys.readouterr().out
    assert read[-1] == cli.RECEIPTS


def test_a_pass_that_committed_nothing_prints_no_gap(monkeypatch, capsys):
    read, _ = _a_pass(monkeypatch, [])
    assert cli.cmd_tick("de") == 0 and cli.cmd_tick() == 0
    assert "write gap" not in capsys.readouterr().out and read == []


def test_a_failing_observation_never_stops_the_tick(monkeypatch, capsys):
    """Codex, round 1: the observation is optional. If it raises anything, the
    tick says "not measured" and goes on to OTS, the backup, the heartbeat
    and the digest."""
    _, calls = _a_pass(monkeypatch, COMMITTED, gap=RecursionError("deep"))
    assert cli.cmd_tick() == 0
    assert "[esios-paper:es] write gap: not measured (RecursionError)" in capsys.readouterr().out
    assert calls == ["ots_stamp", "git_backup", "heartbeat", "email_digest"]
    _, calls = _a_pass(monkeypatch, COMMITTED, gap=MemoryError())
    assert cli.cmd_tick("de") == 0 and calls == ["ots_stamp"]
    assert "[esios-paper:de] write gap: not measured (MemoryError)" in capsys.readouterr().out


class _Git:
    """subprocess.run over `git -C REPO …`, recording each call's arguments.
    `staged` is what the name-only listing prints, or an exception it raises."""
    def __init__(self, staged, listing_exit=0):
        self.staged, self.listing_exit, self.calls = staged, listing_exit, []
    def __call__(self, cmd, **kw):
        self.calls.append((cmd[3:], kw.get("timeout")))
        if "--name-only" in cmd:
            if isinstance(self.staged, Exception):
                raise self.staged
            return SimpleNamespace(returncode=self.listing_exit, stdout=self.staged, stderr="fatal")
        quiet_diff = cmd[3:5] == ["diff", "--cached"] and "--quiet" in cmd
        return SimpleNamespace(returncode=1 if quiet_diff else 0, stdout="", stderr="")
    def commits(self):
        return [c for c, _ in self.calls if c[0] == "commit"]


def test_the_gaps_go_in_the_commit_body_and_the_subject_is_unchanged(monkeypatch):
    git = _Git("Data/es/receipts.jsonl\nData/de/receipts.jsonl\n")
    monkeypatch.setattr(cli.subprocess, "run", git)
    monkeypatch.setattr(cli, "last_write_gap", lambda p: {"es": 4_200_000, "de": None}[p.parent.name])
    cli.git_backup("2026-11-16")
    commit, = git.commits()
    assert commit[:3] == ["commit", "-m", "data: tick 2026-11-16"]
    assert commit[3] == "-m" and commit[4] == (
        "Write gap, last receipt per file (assumes the file clock lags ≤10 ms and the clock did "
        "not step; register T-35): es ≤15 ms, de not measured")
    assert "[skip ci]" not in " ".join(commit)
    listing, = [kw for c, kw in git.calls if "--name-only" in c]
    assert listing == 30                                   # the extra git call is bounded


def test_no_receipts_staged_no_body(monkeypatch):
    for staged in ("Data/es/prices.json\n", ""):
        git = _Git(staged)
        monkeypatch.setattr(cli.subprocess, "run", git)
        cli.git_backup("2026-11-16")
        assert git.commits() == [["commit", "-m", "data: tick 2026-11-16"]]


def test_a_failing_observation_never_stops_the_backup(monkeypatch, capsys):
    """Codex, round 1: the extra git call times out or cannot start, or reading
    a file raises: "not measured", then the same commit and push as without
    the observation, subject unchanged and no body."""
    for failure, name in ((subprocess.TimeoutExpired(["git"], 30), "TimeoutExpired"),
                          (FileNotFoundError("git"), "FileNotFoundError")):
        git = _Git(failure)
        monkeypatch.setattr(cli.subprocess, "run", git)
        cli.git_backup("2026-11-16")
        assert f"[esios-paper] write gap: not measured ({name})" in capsys.readouterr().out
        assert git.commits() == [["commit", "-m", "data: tick 2026-11-16"]]
        assert ["push"] in [c for c, _ in git.calls]
    git = _Git("Data/es/receipts.jsonl\n")
    monkeypatch.setattr(cli.subprocess, "run", git)
    monkeypatch.setattr(cli, "last_write_gap", lambda p: (_ for _ in ()).throw(RecursionError("deep")))
    cli.git_backup("2026-11-16")
    assert "not measured (RecursionError)" in capsys.readouterr().out
    assert git.commits() == [["commit", "-m", "data: tick 2026-11-16"]] and ["push"] in [c for c, _ in git.calls]


def _real_repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True,
                              check=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}).stdout
    git("init", "-q", "-b", "main")
    git("config", "user.name", "made up")
    git("config", "user.email", "made-up@example.test")
    monkeypatch.setattr(cli, "REPO", tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("HOME", str(tmp_path))
    return git


def test_on_a_real_repository_the_body_carries_the_gap_and_the_subject_stays(tmp_path, monkeypatch):
    git = _real_repo(tmp_path, monkeypatch)
    f = _receipts(tmp_path / "Data" / "es" / "receipts.jsonl", STAMP)
    _file_time_ns(f, cli._ns(STAMP) + 1_500_000)
    cli.git_backup("2026-11-16")                  # no remote: the push fails and is logged
    assert git("log", "-1", "--format=%s") == "data: tick 2026-11-16\n"
    assert git("log", "-1", "--format=%b").strip() == (
        "Write gap, last receipt per file (assumes the file clock lags ≤10 ms and the clock did "
        "not step; register T-35): es ≤12 ms")


def test_a_deeply_nested_last_line_is_still_committed_on_a_real_repository(tmp_path, monkeypatch, capsys):
    """Codex, round 1: a malformed last line nested deeply enough makes the
    JSON reader raise RecursionError. The receipts still reach the commit."""
    git = _real_repo(tmp_path, monkeypatch)
    f = tmp_path / "Data" / "de" / "receipts.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("[" * 200_000 + "]" * 200_000 + "\n")
    cli.git_backup("2026-11-16")
    assert "[esios-paper] write gap: not measured (RecursionError)" in capsys.readouterr().out
    assert git("log", "-1", "--format=%s") == "data: tick 2026-11-16\n"
    assert git("log", "-1", "--format=%b").strip() == ""
    assert git("show", "--name-only", "--format=", "HEAD").split() == ["Data/de/receipts.jsonl"]


def test_a_discovery_git_that_exits_non_zero_is_said_and_the_backup_goes_on(monkeypatch, capsys):
    """Codex, round 2: a failing listing was taken silently as "nothing
    staged". It says so; the commit and push go on, subject unchanged, no body."""
    git = _Git("Data/es/receipts.jsonl\n", listing_exit=128)
    monkeypatch.setattr(cli.subprocess, "run", git)
    monkeypatch.setattr(cli, "last_write_gap", lambda p: 4_200_000)
    cli.git_backup("2026-11-16")
    assert "[esios-paper] write gap: not measured (git exit 128)" in capsys.readouterr().out
    assert git.commits() == [["commit", "-m", "data: tick 2026-11-16"]]
    assert ["push"] in [c for c, _ in git.calls]
