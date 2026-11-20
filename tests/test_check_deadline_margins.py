"""D-36's triggers T1 and T2 (register T-41): a write gap logged at 100 ms or
more, or a receipt stamped 60 s or less before its market's deadline.

A real git repository is built for each test, with each commit's body carrying
the writer's write-gap line or not, and GitHub's activity record is a fake. The
check must fire T1 on the logged upper bound as logged, fire T2 on the stamp
against the market's own deadline, report a receipt commit with no gap (or
"not measured") as detector health and never as a trigger, take the push-time
check's walk and start date as they are, print nothing from a commit's body and
no price, and write nothing. All numbers below are made up."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import github_shell, private_file, workflow_steps

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_deadline_margins", ROOT / "scripts" / "check_deadline_margins.py")
cdm = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cdm
_SPEC.loader.exec_module(cdm)
cpt = cdm.cpt

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
ES = SimpleNamespace(deadline_hour=13, tz=ZoneInfo("Europe/Madrid"))      # 12:00Z in winter
DE = SimpleNamespace(deadline_hour=12, tz=ZoneInfo("Europe/Berlin"))      # 11:00Z in winter
MARKETS = {"es": ES}
PAIRS = {("battery-2h2h-persistence", "1"), ("battery-2h2h-weekly", "1"), ("battery-2h2h-rankblend", "1")}
ASSUMES = "assumes the file clock lags ≤10 ms and the clock did not step"


def gap(*entries: str) -> str:
    """A body line in the writers' format, e.g. gap("es ≤10 ms")."""
    return f"Write gap, last receipt per file ({ASSUMES}; register T-35): " + ", ".join(entries)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_DATE": "2026-11-15T10:00:00Z",
                               "GIT_COMMITTER_DATE": "2026-11-15T10:00:00Z"}).stdout.strip()


def _receipt(day: str, stamp: str, strategy: str = "battery-2h2h-persistence") -> dict:
    return {"target": "2026-11-16", "basis_day": day, "strategy": strategy, "strategy_version": "1",
            "basis_profile": {"0": 87.6543}, "committed_at": stamp}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "made up")
    _git(r, "config", "user.email", "made-up@example.test")
    (r / "README").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "start")
    return r


def _append(repo: Path, *receipts: dict, body: str | None = None, slugs=("es",)) -> str:
    """One commit adding `receipts` to each market in `slugs`; `body`, if any,
    is the commit's body (its second -m)."""
    for slug in slugs:
        f = repo / "Data" / slug / "receipts.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as out:
            for r in receipts:
                out.write(json.dumps(r) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "data: tick", *(["-m", body] if body is not None else []))
    return _git(repo, "rev-parse", "HEAD")


def _push(at: str, before: str, after: str) -> dict:
    return {"timestamp": at, "before": before, "after": after, "activity_type": "push",
            "ref": "refs/heads/main"}


def _get(*pages):
    def get(url, token):
        get.n += 1
        n = get.n - 1
        return pages[n], (f"{cpt.API}repos/o/r/activity?page={n + 2}" if n + 1 < len(pages) else None)
    get.n = 0
    return get


def _old(start: str) -> dict:
    """A push long before the window, so GitHub's history reaches back past it."""
    return _push("2026-11-01T00:00:00Z", "", start)


def _run(repo, pages, capsys, trigger, markets=MARKETS, start=None, days=7, items=None):
    rc, word, got = cdm.run(repo, "o/r", "HEAD", NOW, days, markets, "tok", trigger, get=_get(*pages),
                            start=start, pairs=PAIRS)
    if items is not None:
        items.extend(got)
    return rc, word, capsys.readouterr().out


# --- the body line ---

@pytest.mark.parametrize("body, slug, expected", [
    (gap("es ≤10 ms"), "es", 10),
    (gap("de ≤10 ms", "es ≤140 ms", "fr ≤10 ms"), "es", 140),
    (gap("de ≤10 ms", "es ≤140 ms"), "de", 10),
    (gap("es not measured"), "es", "not measured"),
    (gap("es not measured: the file's time is before the stamp (the clock stepped?)", "de ≤10 ms"), "es",
     "not measured"),
    (gap("es not measured: the file's time is before the stamp (the clock stepped?)", "de ≤10 ms"), "de", 10),
    (gap("de ≤10 ms"), "es", "no line"),                 # the line, but not for this market
    ("", "es", "no line"),
    ("Write gap: es ≤10 ms", "es", "no line"),           # not the writers' line
    (gap("es ≤ten ms"), "es", "no line"),                # an entry that is not a number
    (gap("ese ≤10 ms"), "es", "no line"),                # another market's name that starts alike
    ("some text\n" + gap("es ≤200 ms") + "\n" + gap("es ≤10 ms"), "es", 200),   # the first line only
])
def test_the_gap_is_read_from_the_writers_own_line(body, slug, expected):
    assert cdm.gap_ms(body, slug) == expected


def test_the_thresholds_are_the_ruled_ones():
    """D-36, ruled 2026-11-20: T1 a gap of 100 ms or more, T2 a stamp within 60 s."""
    assert (cdm.T1_MS, cdm.T2_S) == (100, 60)


# --- T2 (the stamp-margin check) ---

def test_t2_fires_for_a_stamp_60_s_or_less_before_the_deadline(repo, capsys):
    """Ruled (round 1): "within 60 s" is inclusive, so 60.0 s fires."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T11:59:00.5+00:00"),      # 59.5 s before 12:00Z
                _receipt("2026-11-15", "2026-11-15T11:59:00+00:00", "battery-2h2h-weekly"),   # 60 s: T2 (inclusive)
                _receipt("2026-11-15", "2026-11-15T11:58:59.5+00:00", "battery-2h2h-rankblend"),   # 60.5 s: not
                body=gap("es ≤10 ms"))
    rc, word, out = _run(repo, [[_push("2026-11-15T11:59:30Z", start, a), _old(start)]], capsys, "T2")
    assert (rc, word) == (1, "T2")
    assert ("[stamp-margin] es: TRIGGER T2 2026-11-16 [battery-2h2h-persistence v1] stamped "
            "2026-11-15T11:59:00.5+00:00, deadline 2026-11-15T12:00:00+00:00, 59.5 s before it") in out
    assert "[battery-2h2h-weekly v1] stamped 2026-11-15T11:59:00+00:00" in out and "60.0 s before it" in out
    assert "[stamp-margin] es: receipts 3, T2 2, CANNOT JUDGE (not a trigger) 0" in out
    assert "battery-2h2h-rankblend" not in out                          # 60.5 s: outside


def test_t2_uses_the_markets_own_hour_and_zone(repo, capsys):
    """The same stamp is 30 s before Berlin's 12:00 (11:00Z) and an hour before
    Madrid's 13:00 (12:00Z)."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:59:30+00:00"), body=gap("de ≤10 ms", "es ≤10 ms"),
                slugs=("de", "es"))
    rc, word, out = _run(repo, [[_push("2026-11-15T11:00:30Z", start, a), _old(start)]], capsys, "T2",
                         markets={"de": DE, "es": ES})
    assert (rc, word) == (1, "T2")
    assert "de: TRIGGER T2" in out and "es: TRIGGER T2" not in out and "30.0 s before it" in out


def test_a_stamp_at_or_after_the_deadline_is_t2_and_says_after(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T12:00:02+00:00"),
                _receipt("2026-11-15", "2026-11-15T12:00:00+00:00", "battery-2h2h-weekly"), body=gap("es ≤10 ms"))
    rc, word, out = _run(repo, [[_push("2026-11-15T12:01:00Z", start, a), _old(start)]], capsys, "T2")
    assert (rc, word) == (1, "T2") and "2.0 s after it" in out and "0.0 s after it" in out


def test_the_t2_check_ignores_the_gap_and_the_t1_check_ignores_the_stamp(repo, capsys):
    """One check per trigger (C1): each judges its own trigger only, so each
    red-main issue means one thing."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T11:59:50+00:00"))          # T2, and no gap line
    pages = [[_push("2026-11-15T11:59:55Z", start, a), _old(start)]]
    rc, word, out = _run(repo, pages, capsys, "T2")
    assert (rc, word) == (1, "T2") and "write gap" not in out and "write-gap" not in out
    rc, word, out = _run(repo, pages, capsys, "T1")
    assert (rc, word) == (2, "T1 CANNOT JUDGE (not a trigger)") and "TRIGGER T2" not in out and "stamped" not in out


def test_a_receipt_without_a_readable_stamp_or_day_is_cannot_judge(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    bad = [{"basis_day": "2026-11-15", "committed_at": "2026-11-15T10:00:00"},   # no zone
           {"basis_day": "soon", "committed_at": "2026-11-15T10:00:00+00:00"},
           {"committed_at": 7}]
    a = _append(repo, *bad, body=gap("es ≤10 ms"))
    pages = [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]]
    rc, word, out = _run(repo, pages, capsys, "T2")
    assert (rc, word) == (2, "CANNOT JUDGE (not a trigger)")
    assert out.count("has no readable stamp or day") == 3
    assert _run(repo, pages, capsys, "T1")[:2] == (0, "NONE FIRED")    # T1 needs only the gap


# --- T1 (the write-gap check) ---

@pytest.mark.parametrize("logged, fires", [(99, False), (100, True), (101, True), (10, False)])
def test_t1_compares_the_logged_upper_bound_to_100_ms_as_logged(repo, capsys, logged, fires):
    """Ruled 2026-11-20: the logged figure (gap + 10 ms, rounded up) is compared
    as is, nothing subtracted: errs toward reporting."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap(f"es ≤{logged} ms"))
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]], capsys, "T1")
    assert (rc, word) == ((1, "T1") if fires else (0, "NONE FIRED"))
    assert (f"[write-gap] es: TRIGGER T1 commit {a[:7]}: write gap ≤{logged} ms logged (an upper bound; "
            "T1 is 100 ms or more)" in out) == fires


def test_t1_is_judged_per_market_in_a_commit_that_carries_several(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T09:00:00+00:00"),
                body=gap("de ≤10 ms", "es ≤250 ms"), slugs=("de", "es"))
    rc, word, out = _run(repo, [[_push("2026-11-15T09:01:00Z", start, a), _old(start)]], capsys, "T1",
                         markets={"de": DE, "es": ES})
    assert (rc, word) == (1, "T1") and "es: TRIGGER T1" in out and "de: TRIGGER" not in out
    assert "[write-gap] es: commits 1, T1 1, T1 CANNOT JUDGE (not a trigger) 0, CANNOT JUDGE (not a trigger) 0" in out


def test_t1_with_detector_health_in_the_same_run(repo, capsys):
    """Exit 1 wins over 2, and the also-line says the run could not judge everything."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T09:00:00+00:00"), body=gap("es ≤300 ms"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00", "battery-2h2h-weekly"))   # no line
    rc, word, out = _run(repo, [[_push("2026-11-15T10:00:30Z", a, b), _push("2026-11-15T09:01:00Z", start, a),
                                 _old(start)]], capsys, "T1")
    assert (rc, word) == (1, "T1")
    assert out.splitlines()[-1] == "[write-gap] also CANNOT JUDGE: 1"
    assert f"es: T1 CANNOT JUDGE (not a trigger): commit {b[:7]} carries no write-gap entry for this market" in out


@pytest.mark.parametrize("body, said", [
    (None, "no write-gap entry for this market"),
    ("something else entirely", "no write-gap entry for this market"),
    (gap("de ≤10 ms"), "no write-gap entry for this market"),
    (gap("es not measured (no line from the tick)"), 'the gap "not measured"'),
    (gap("es not measured: the file's time is before the stamp (the clock stepped?)"), 'the gap "not measured"'),
])
def test_a_receipt_commit_without_a_gap_is_t1_cannot_judge_never_a_trigger(repo, capsys, body, said):
    """Ruled 2026-11-20: exit 2, labelled exactly "T1 CANNOT JUDGE (not a
    trigger)". Detector health: it never reopens D-36."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=body)
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]], capsys, "T1")
    assert (rc, word) == (2, "T1 CANNOT JUDGE (not a trigger)")
    assert f"es: T1 CANNOT JUDGE (not a trigger): commit {a[:7]} carries {said}" in out
    assert "TRIGGER" not in out


def test_only_the_commit_body_is_read_never_its_subject(repo, capsys):
    """Ruled (round 1): the writers put the line in the body; a subject that
    looks like it, with an empty body, is no gap line."""
    start = _git(repo, "rev-parse", "HEAD")
    f = repo / "Data/es/receipts.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(_receipt("2026-11-15", "2026-11-15T10:00:00+00:00")) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", gap("es ≤10 ms"))                # the line as the SUBJECT, no body
    a = _git(repo, "rev-parse", "HEAD")
    assert cdm._body(repo, a).strip() == ""
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]], capsys, "T1")
    assert (rc, word) == (2, "T1 CANNOT JUDGE (not a trigger)")
    assert f"commit {a[:7]} carries no write-gap entry for this market" in out


def test_a_commit_without_receipts_needs_no_gap(repo, capsys):
    """Only a commit that added receipts to a market's file is judged: the
    server's 17:00 pass, a docs change, a ledger line, need no gap line."""
    start = _git(repo, "rev-parse", "HEAD")
    (repo / "notes").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    a = _git(repo, "rev-parse", "HEAD")
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]], capsys, "T1")
    assert (rc, word) == (0, "NONE FIRED") and "[write-gap] es: commits 0, T1 0" in out


def test_a_receipt_merged_from_a_branch_has_no_gap_line_and_says_so(repo, capsys):
    """The push-time check's walk attributes a branch's receipt to the merge
    commit on main; a merge carries no gap line: T1 CANNOT JUDGE (stated limit)."""
    start = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "side")
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap("es ≤10 ms"))
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    m = _git(repo, "rev-parse", "HEAD")
    rc, word, out = _run(repo, [[_push("2026-11-15T10:30:00Z", start, m), _old(start)]], capsys, "T1")
    assert (rc, word) == (2, "T1 CANNOT JUDGE (not a trigger)") and f"commit {m[:7]}" in out


def test_t1_health_alone_is_its_own_word_and_other_health_wins(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"))                 # no line
    (repo / "Data/es/receipts.jsonl").write_text("rewritten\n")
    _git(repo, "commit", "-qam", "rewrite", "-m", gap("es ≤10 ms"))
    b = _git(repo, "rev-parse", "HEAD")
    rc, word, out = _run(repo, [[_push("2026-11-15T10:05:00Z", a, b), _push("2026-11-15T10:01:00Z", start, a),
                                 _old(start)]], capsys, "T1")
    assert (rc, word) == (2, "CANNOT JUDGE (not a trigger)")
    assert "T1 CANNOT JUDGE (not a trigger): commit" in out and f"commit {b[:7]}: the file there" in out


# --- for both checks ---

@pytest.mark.parametrize("trigger", ["T1", "T2"])
def test_a_history_that_cannot_be_read_or_shown_complete_is_cannot_judge(repo, capsys, trigger):
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap("es ≤10 ms"))
    def broken(url, token):
        raise OSError("down")
    rc, word, items = cdm.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", trigger, get=broken, pairs=PAIRS)
    out = capsys.readouterr().out
    assert (rc, word) == (2, "CANNOT JUDGE (not a trigger)") and "cannot be read from GitHub" in out
    assert items == [("", "CANNOT JUDGE (not a trigger): the repository's push history cannot be read from GitHub")]
    rc, word, out = _run(repo, [[]], capsys, trigger)                 # a history that ends before the window
    assert (rc, word) == (2, "CANNOT JUDGE (not a trigger)") and "ends before the window's start" in out


@pytest.mark.parametrize("trigger", ["T1", "T2"])
def test_before_the_start_date_is_information_and_sets_nothing(repo, capsys, trigger):
    """The history before the ruling (e.g. JP's 12 receipts) is listed, never
    the verdict, and never in the email; GitHub's push time decides, never the
    stamp."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T11:59:50+00:00"))          # T2 and no gap line
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap("es ≤10 ms"))
    items: list = []
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", a, b), _push("2026-11-14T11:59:55Z", start, a),
                                 _old(start)]], capsys, trigger, start=datetime(2026, 11, 15, tzinfo=timezone.utc),
                         items=items)
    assert (rc, word) == (0, "NONE FIRED") and items == []
    assert out.count("(before the start date: information)") == 1
    summary = ("es: receipts 1, T2 0, CANNOT JUDGE (not a trigger) 0; before the start date: T2 1, "
               "CANNOT JUDGE (not a trigger) 0" if trigger == "T2" else
               "es: commits 1, T1 0, T1 CANNOT JUDGE (not a trigger) 0, CANNOT JUDGE (not a trigger) 0; "
               "before the start date: T1 0, T1 CANNOT JUDGE (not a trigger) 1, CANNOT JUDGE (not a trigger) 0")
    assert summary in out


def test_a_receipt_with_no_push_in_the_record_always_sets_the_verdict(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap("es ≤10 ms"))
    _append(repo, _receipt("2026-11-15", "2026-11-15T11:59:59+00:00"), body=gap("es ≤10 ms"))   # never pushed
    rc, word, out = _run(repo, [[_push("2026-11-15T10:01:00Z", start, a), _old(start)]], capsys, "T2",
                         start=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert (rc, word) == (1, "T2") and "information" not in out.split("TRIGGER T2", 1)[1].splitlines()[0]


def test_the_walk_is_the_push_time_checks_own(repo, capsys, monkeypatch):
    """One implementation: the check takes cpt.collect's receipts and push
    times and nothing else."""
    seen = []
    real = cpt.collect
    def spy(*a, **k):
        seen.append(k.get("until"))
        return real(*a, **k)
    monkeypatch.setattr(cpt, "collect", spy)
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:00+00:00"), body=gap("es ≤10 ms"))
    until = datetime(2026, 11, 15, 11, tzinfo=timezone.utc)
    for trigger in ("T1", "T2"):
        rc, word, _ = cdm.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", trigger, until=until, pairs=PAIRS,
                              get=_get([_push("2026-11-15T10:01:00Z", start, a), _old(start)]))
        assert (rc, word) == (0, "NONE FIRED")
    assert seen == [until, until]
    capsys.readouterr()
    with pytest.raises(ValueError):
        cdm.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", "T3", pairs=PAIRS, get=_get([]))


# --- the items for the email (C2) ---

def test_the_items_are_what_set_the_verdict_newest_first(repo, capsys, tmp_path):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-13", "2026-11-13T11:59:50+00:00"), body=gap("es ≤10 ms"))
    b = _append(repo, {**_receipt("2026-11-15", "2026-11-15T11:59:40+00:00"), "target": "2026-11-17"},
                body=gap("es ≤10 ms"))
    c = _append(repo, {"basis_day": "soon", "committed_at": "x"}, body=gap("es ≤10 ms"))
    items: list = []
    rc, word, out = _run(repo, [[_push("2026-11-15T12:30:00Z", b, c), _push("2026-11-15T11:59:45Z", a, b),
                                 _push("2026-11-13T11:59:55Z", start, a), _old(start)]], capsys, "T2", items=items)
    assert rc == 1 and len(items) == 3
    f = tmp_path / "items.txt"
    cpt.write_items_to(f, items)
    lines = f.read_text().splitlines()
    assert lines[0].startswith("2026-11-17 es: TRIGGER T2 2026-11-17") and lines[1].startswith("2026-11-16 es: TRIGGER T2")
    assert lines[2] == f"- es: CANNOT JUDGE (not a trigger): a receipt of commit {c[:7]} has no readable stamp or day"


def test_main_writes_the_items_file_only_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(cdm, "run", lambda *a, **k: (1, "T2", [("2026-11-21", "es: TRIGGER T2 x")]))
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    f = tmp_path / "items.txt"
    assert cdm.main(["--trigger", "T2"], now=NOW) == (1, "T2", "[stamp-margin]") and not f.exists()
    assert cdm.main(["--trigger", "T2", "--items", str(f)], now=NOW)[0] == 1
    assert f.read_text() == "2026-11-21 es: TRIGGER T2 x\n"


@pytest.mark.parametrize("check", ["T1", "T2", "push-times"])
def test_the_items_file_never_overwrites_an_existing_file(monkeypatch, tmp_path, capsys, check):
    """Ruled (round 1): --items is created exclusively, before the check runs,
    so a mistaken path can never truncate a receipts or ledger file; the run
    is refused (argparse, exit 2) and the file is untouched."""
    ran = []
    monkeypatch.setattr(cdm, "run", lambda *a, **k: ran.append(1) or (0, "NONE FIRED", []))
    monkeypatch.setattr(cpt, "run", lambda *a, **k: ran.append(1) or 0)
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(b'{"made up": 1}\n')
    args = ["--items", str(ledger)]
    with pytest.raises(SystemExit) as refused:
        cdm.main(["--trigger", check, *args], now=NOW) if check != "push-times" else cpt.main(args, now=NOW)
    assert refused.value.code == 2 and "never overwrites" in capsys.readouterr().err
    assert ledger.read_bytes() == b'{"made up": 1}\n' and ran == []
    for bad in (tmp_path, tmp_path / "no-folder" / "items.txt"):     # a folder; a missing parent
        with pytest.raises(SystemExit):
            cdm.main(["--trigger", "T2", "--items", str(bad)], now=NOW)
    assert ran == [] and not (tmp_path / "no-folder").exists()


@pytest.mark.parametrize("check", ["T2", "push-times"])
def test_a_check_that_crashes_leaves_no_items_file(monkeypatch, tmp_path, capsys, check):
    """Round 2 (Codex): the file is created only after the check returned, so
    a crash leaves none, and the email says "no list", never "0 items"."""
    def crash(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(cdm, "run", crash)
    monkeypatch.setattr(cpt, "run", crash)
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    f = tmp_path / "items.txt"
    args = ["--items", str(f)]
    rc = cdm.entry(["--trigger", check, *args]) if check != "push-times" else cpt.entry(args)
    assert rc == 2 and not f.exists()
    subject, _ = tem.compose(None, "CANNOT JUDGE (not a trigger): the check did not finish\n", ENV)
    assert subject.endswith(": no list")


@pytest.mark.parametrize("check", ["T1", "T2", "push-times"])
def test_a_path_claimed_between_the_check_and_the_write_keeps_the_verdict(monkeypatch, tmp_path, capsys, check):
    """Round 3 (Codex): if the list cannot be created after the check returned,
    a found trigger keeps its exit 1 and its result word; one fixed line says
    so, the file that claimed the path is untouched, and none of ours is left."""
    f = tmp_path / "items.txt"
    def claim_then_fire(*a, **k):
        f.write_bytes(b"claimed meanwhile\n")
        return 1, check, [("2026-11-20", "x")]
    if check == "push-times":
        def fire(*a, items=None, **k):
            f.write_bytes(b"claimed meanwhile\n")
            items.append(("2026-11-20", "x"))
            return 1
        monkeypatch.setattr(cpt, "run", fire)
    else:
        monkeypatch.setattr(cdm, "run", claim_then_fire)
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    args = ["--items", str(f)]
    rc = cdm.entry(["--trigger", check, *args]) if check != "push-times" else cpt.entry(args)
    out = capsys.readouterr().out.splitlines()
    tag = {"T1": "[write-gap]", "T2": "[stamp-margin]", "push-times": "[push-times]"}[check]
    word = {"T1": "T1", "T2": "T2", "push-times": "UNVERIFIABLE"}[check]
    assert rc == 1 and out[-1] == f"{tag} result: {word}"
    assert f"{tag} could not write the items list: FileExistsError" in out
    assert f.read_bytes() == b"claimed meanwhile\n"


class _BrokenWrite:
    """A file object for a file really created at `path`, whose write fails;
    `then` runs first (e.g. another process replacing the path)."""
    def __init__(self, path, then=None):
        self.f, self.then = path.open("x", encoding="utf-8"), then

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()
        return False

    def write(self, text):
        self.f.write(text[:4])
        self.f.flush()
        if self.then:
            self.then()
        raise OSError("disk full")


def test_a_write_that_fails_keeps_the_verdict_and_deletes_nothing(monkeypatch, tmp_path, capsys):
    """Round 4 (Codex): no cleanup at all. A failed write keeps rc and the
    result word; the partial list of ours stays (stated limit)."""
    f = tmp_path / "items.txt"
    monkeypatch.setattr(cpt, "open_items", lambda path: _BrokenWrite(path))
    monkeypatch.setattr(cdm, "run", lambda *a, **k: (1, "T2", [("2026-11-20", "es: TRIGGER T2 x")]))
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    rc = cdm.entry(["--trigger", "T2", "--items", str(f)])
    out = capsys.readouterr().out.splitlines()
    assert rc == 1 and out[-1] == "[stamp-margin] result: T2"
    assert "[stamp-margin] could not write the items list: OSError" in out
    assert f.read_text() == "2026"                                   # ours, partial, not deleted


def test_a_path_replaced_after_creation_is_never_removed(monkeypatch, tmp_path, capsys):
    """Round 4 (Codex): another process puts its file at the path after our
    exclusive create, then our write fails: its file stays."""
    f = tmp_path / "items.txt"
    other = tmp_path / "other"
    other.write_bytes(b"another process's file\n")
    monkeypatch.setattr(cpt, "open_items", lambda path: _BrokenWrite(path, then=lambda: os.replace(other, f)))
    cpt.write_items_to(f, [("2026-11-20", "x")], "[write-gap]")
    assert f.read_bytes() == b"another process's file\n"
    assert capsys.readouterr().out == "[write-gap] could not write the items list: OSError\n"


def test_the_list_is_written_in_one_call(monkeypatch, tmp_path):
    calls = []
    class Spy:
        def __init__(self, path):
            self.f = path.open("x", encoding="utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.f.close()
        def write(self, text):
            calls.append(text)
            self.f.write(text)
    monkeypatch.setattr(cpt, "open_items", Spy)
    cpt.write_items_to(tmp_path / "i.txt", [("2026-11-20", "a"), ("2026-11-19", "b"), ("", "c")])
    assert calls == ["2026-11-20 a\n2026-11-19 b\n- c\n"]


def test_a_date_not_in_its_shape_sorts_last_and_keeps_its_line(tmp_path):
    """Round 2 (Codex): "<invalid>" sorted above real dates; it is no date."""
    f = tmp_path / "items.txt"
    cpt.write_items_to(f, [("<invalid>", "es: TRIGGER T2 <invalid> x"), ("2026-11-20", "y"), ("", "z")])
    assert f.read_text().splitlines() == ["2026-11-20 y", "- z", "- es: TRIGGER T2 <invalid> x"]


def test_the_push_time_check_lists_its_items_too(tmp_path, capsys):
    """C2 applies to receipt-push-times (T3/T4): verdict-setting lines only."""
    r = tmp_path / "pt"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "made up")
    _git(r, "config", "user.email", "made-up@example.test")
    (r / "README").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "start")
    start = _git(r, "rev-parse", "HEAD")
    a = _append(r, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(r, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    _append(r, _receipt("2026-11-15", "2026-11-15T10:00:02+00:00", "battery-2h2h-weekly"))   # never pushed
    items: list = []
    rc = cpt.run(r, "o/r", "HEAD", NOW, 7, MARKETS, "tok", pairs=PAIRS, items=items,
                 start=datetime(2026, 11, 15, tzinfo=timezone.utc),
                 get=_get([_push("2026-11-15T23:00:00Z", a, b), _push("2026-11-14T13:01:00Z", start, a),
                           _old(start)]))
    capsys.readouterr()
    assert rc == 1
    assert [d for d, _ in items] == ["2026-11-16", "2026-11-16"]       # T3 and the never-pushed one; not a's
    assert any("UNVERIFIABLE" in line for _, line in items) and any("CANNOT JUDGE" in line for _, line in items)
    assert not any("information" in line for _, line in items)


# --- what is printed ---

def test_nothing_from_a_commit_body_or_a_price_is_printed(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    hostile = gap("es not measured <script>secret-87.6543</script>")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T11:59:50+00:00", "made-up-strategy"), body=hostile)
    pages = [[_push("2026-11-15T12:01:00Z", start, a), _old(start)]]
    out = _run(repo, pages, capsys, "T2")[2] + _run(repo, pages, capsys, "T1")[2]
    assert "secret" not in out and "87.65" not in out and "<script>" not in out
    assert "[<invalid> v<invalid>]" in out                   # an unregistered strategy is not echoed


@pytest.mark.parametrize("trigger, tag", [("T1", "[write-gap]"), ("T2", "[stamp-margin]")])
def test_entry_prints_the_result_line_and_never_exits_1_for_a_crash(monkeypatch, capsys, trigger, tag):
    for rc, word in ((0, "NONE FIRED"), (1, trigger), (2, "CANNOT JUDGE (not a trigger)")):
        monkeypatch.setattr(cdm, "main", lambda argv=None, rc=rc, word=word: (rc, word, tag))
        assert cdm.entry(["--trigger", trigger]) == rc
        assert capsys.readouterr().out.splitlines()[-1] == f"{tag} result: {word}"
    def crash(argv=None):
        raise KeyError("x")
    monkeypatch.setattr(cdm, "main", crash)
    assert cdm.entry(["--trigger", trigger]) == 2
    assert capsys.readouterr().out.splitlines() == [f"{tag} CANNOT JUDGE: unexpected error (KeyError)",
                                                    f"{tag} result: CANNOT JUDGE (not a trigger)"]


def test_main_reads_its_arguments_and_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(cdm, "run", lambda repo_dir, repo, ref, now, days, markets, token, trigger, start=None,
                        until=None: seen.update(repo=repo, ref=ref, days=days, token=token, trigger=trigger,
                                                start=start, until=until) or (0, "NONE FIRED", []))
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(SystemExit):
        cdm.main(["--trigger", "T1"])
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with pytest.raises(SystemExit):
        cdm.main([])                                            # --trigger is required
    with pytest.raises(SystemExit):
        cdm.main(["--trigger", "T3"])
    assert cdm.main(["--trigger", "T1", "--days", "3", "--ref", "origin/main", "--since", "2026-11-21",
                     "--until", "2026-11-21T18:47:03Z"], now=NOW) == (0, "NONE FIRED", "[write-gap]")
    assert seen == {"repo": "o/r", "ref": "origin/main", "days": 3, "token": "tok", "trigger": "T1",
                    "start": datetime(2026, 11, 21, tzinfo=timezone.utc),
                    "until": datetime(2026, 11, 21, 18, 47, 3, tzinfo=timezone.utc)}
    assert cdm.main(["--trigger", "T2", "--since", "2026-11-21T09:30:00+00:00"], now=NOW)[0] == 0
    assert seen["start"] == datetime(2026, 11, 21, 9, 30, tzinfo=timezone.utc)
    for bad in (["--since", "next week"], ["--since", "2026-11-21T09:30:00"], ["--until", "2026-11-21T18:47:03"]):
        with pytest.raises(SystemExit):
            cdm.main(["--trigger", "T2", *bad], now=NOW)


def test_a_run_writes_nothing_in_a_fresh_checkout(tmp_path):
    """Run as a workflow runs it (python -I): the sibling import and the
    registry write no __pycache__."""
    fresh = tmp_path / "fresh"
    (fresh / "scripts").mkdir(parents=True)
    for name in ("check_deadline_margins.py", "check_receipt_push_times.py"):
        shutil.copy(ROOT / "scripts" / name, fresh / "scripts")
    shutil.copytree(ROOT / "src" / "talea", fresh / "src" / "talea", ignore=shutil.ignore_patterns("__pycache__"))
    _git(fresh, "init", "-q", "-b", "main")
    _git(fresh, "config", "user.name", "made up")
    _git(fresh, "config", "user.email", "made-up@example.test")
    _git(fresh, "add", "-A")
    _git(fresh, "commit", "-qm", "fresh")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HTTPS_PROXY": "http://localhost:9",
           "https_proxy": "http://localhost:9", "NO_PROXY": "", "no_proxy": ""}
    for trigger, tag in (("T1", "[write-gap]"), ("T2", "[stamp-margin]")):
        r = subprocess.run([sys.executable, "-I", "scripts/check_deadline_margins.py", "--trigger", trigger,
                            "--repo", "o/r"], cwd=fresh, capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 2 and "cannot be read from GitHub" in r.stdout, r.stdout + r.stderr
        assert r.stdout.splitlines()[-1] == f"{tag} result: CANNOT JUDGE (not a trigger)"
    assert _git(fresh, "status", "--porcelain", "--ignored") == ""
    assert not list(fresh.rglob("__pycache__"))


# --- the two workflows (register T-41, C1) ---

CHECKS = {"T1": ("write-gap.yml", "[write-gap]", "41 18 * * *"),
          "T2": ("stamp-margin.yml", "[stamp-margin]", "53 18 * * *")}


def _workflow(trigger: str) -> str:
    return private_file(f".github/workflows/{CHECKS[trigger][0]}").read_text()


def _check_step(trigger: str) -> dict:
    return next(s for s in workflow_steps(_workflow(trigger)) if (s["run"] or "").startswith('until="$(date'))


def _step_script(trigger: str) -> str:
    return _check_step(trigger)["run"]


def test_no_workflow_has_a_plain_value_that_yaml_reads_as_a_mapping():
    """Found by CI on this PR's first push: a step named "No slow write: every
    …" is not YAML (a plain value may not hold ": "), and GitHub refused the
    whole workflow ("a workflow file issue"), so the check would never have
    run. The project has no YAML parser (standard library only); this catches
    the class on the lines where it bites: `name:` and other one-line keys."""
    bad = []
    for path in sorted(private_file(".github/workflows/ci.yml").parent.glob("*.yml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            m = re.match(r"^\s*(?:- )?(name|description|group|if|runs-on|uses):\s+(.*)$", line)
            if m and not m.group(2).startswith(("'", '"', "${{", "|", ">")) and ": " in m.group(2).split(" #")[0]:
                bad.append(f"{path.name}:{n}")
    assert not bad, bad
    assert re.match(r"^\s*(?:- )?(name):\s+(.*)$", "      - name: No slow write: every gap")   # the guard bites


@pytest.mark.parametrize("trigger", sorted(CHECKS))
def test_each_workflow_is_read_only_daily_and_runs_its_own_trigger(trigger):
    """Once a day: each red run emails (C2), so a check that ran after every
    writer would email many times a day."""
    file, tag, cron = CHECKS[trigger]
    text = _workflow(trigger)
    assert text.startswith(f"name: {file.removesuffix('.yml')}\n")
    assert re.search(r"^permissions:\n  contents: read\n\nconcurrency:", text, re.M)
    assert text.count("permissions:") == 1                      # no job widens it
    on = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on, re.M) == ["schedule", "workflow_dispatch"]
    assert re.findall(r'- cron: "([^"]+)"', text) == [cron]
    assert "fetch-depth: 0" in text
    script = _step_script(trigger)
    assert [s["shell"] for s in workflow_steps(text) if s["run"] and " | " in s["run"]] == ["bash"]   # pipefail
    assert (f'uv run --no-project python -I scripts/check_deadline_margins.py --trigger {trigger} \\\n'
            '  --repo "$GITHUB_REPOSITORY" --ref origin/main --days 7 --since "$start" --until "$until" \\\n'
            '  --items "$RUNNER_TEMP/items.txt" 2>&1 | tee "$log"') in script
    assert f"-- .github/workflows/{file})" in script
    assert script.index('until="$(date') < script.index("git fetch")
    email = text.split("- name: Email the operator what set the verdict (red runs only)\n", 1)[1]
    assert email.startswith("        if: ${{ failure() }}\n")
    assert 'run: python3 -I scripts/trigger_email.py "$RUNNER_TEMP/items.txt" "$RUNNER_TEMP/label.txt"' in email


def test_the_push_time_workflow_emails_its_red_runs_too():
    text = private_file(".github/workflows/receipt-push-times.yml").read_text()
    email = text.split("- name: Email the operator what set the verdict (red runs only)\n", 1)[1]
    assert email.startswith("        if: ${{ failure() }}\n")
    assert 'run: python3 -I scripts/trigger_email.py "$RUNNER_TEMP/items.txt" "$RUNNER_TEMP/label.txt"' in email
    assert re.findall(r'- cron: "([^"]+)"', text) == ["23 18 * * *"]


def _run_step(tmp_path, trigger: str, check_exit: int, lines: list[str], merged: str = "2026-11-21T09:00:00+00:00"):
    """The step's script as GitHub runs it, with stand-ins: date answers its
    two questions (the time before the fetch; the merge plus 30 minutes, here
    made up), git's log answers `merged` (empty: the merge is not found), uv
    prints `lines` and exits `check_exit`."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    say = "".join(f"echo '{l}'; " for l in lines)
    for name, body in (("date", 'echo "date $*" >> "$LOG"; case "$*" in *"30 minutes"*) '
                                'echo 2026-11-21T09:30:00Z;; *) echo 2026-11-21T18:47:03Z;; esac'),
                       ("git", f'echo "git $*" >> "$LOG"; [ "$1" = log ] && echo "{merged}"; exit 0'),
                       ("uv", f'echo "uv $*" >> "$LOG"; {say}exit {check_exit}')):
        (bin_ / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_ / name).chmod(0o755)
    step = _check_step(trigger)
    r = subprocess.run([*github_shell(step), step["run"]],          # as GitHub runs it
                       capture_output=True, text=True, env={"PATH": f"{bin_}:/usr/bin:/bin", "LOG": str(log),
                                                            "GITHUB_REPOSITORY": "o/r", "RUNNER_TEMP": str(tmp_path)})
    label = tmp_path / "label.txt"
    return r, log.read_text().splitlines(), (label.read_text() if label.exists() else None)


def _cases(trigger: str) -> list:
    tag = CHECKS[trigger][1]
    R = f"{tag} result: "
    fired = f"TRIGGER {trigger} (D-36): report to the operator the same day"
    unfinished = "CANNOT JUDGE (not a trigger): the check did not finish (exit {})"
    other = "T2" if trigger == "T1" else "T1"
    cases = [
        (0, [R + "NONE FIRED"], 0, None),
        (1, [R + trigger], 1, fired),
        (1, [f"{tag} also CANNOT JUDGE: 2", R + trigger], 1,
         fired + "; and CANNOT JUDGE (not a trigger): detector health"),
        (1, [f"{tag} es: also CANNOT JUDGE: 2", R + trigger], 1, fired),          # a look-alike line
        (2, [R + "CANNOT JUDGE (not a trigger)"], 2, "CANNOT JUDGE (not a trigger): detector health; it never reopens D-36"),
        (1, [], 2, unfinished.format(1)),                  # uv failed, a crash
        (0, [], 2, unfinished.format(0)),                  # a 0 without its line
        (1, [R + "NONE FIRED"], 2, unfinished.format(1)),
        (0, [R + trigger], 2, unfinished.format(0)),
        (1, [R + other], 2, unfinished.format(1)),         # the other check's word
        (1, ["[deadline-margins] result: " + trigger], 2, unfinished.format(1)),   # another tag
    ]
    t1_health = (2, [R + "T1 CANNOT JUDGE (not a trigger)"], 2,
                 "T1 CANNOT JUDGE (not a trigger): detector health; it never reopens D-36")
    cases.append(t1_health if trigger == "T1" else (2, t1_health[1], 2, unfinished.format(2)))
    return [(trigger, *c) for c in cases]


@pytest.mark.parametrize("trigger, check_exit, lines, step_exit, label", _cases("T1") + _cases("T2"))
def test_the_step_trusts_only_the_checks_last_line(tmp_path, trigger, check_exit, lines, step_exit, label):
    r, calls, written = _run_step(tmp_path, trigger, check_exit, lines)
    assert r.returncode == step_exit, (r.stdout, r.stderr)
    assert [c.split()[0] for c in calls] == ["date", "git", "git", "date", "uv"]
    assert calls[0] == "date -u -d 2 seconds ago +%Y-%m-%dT%H:%M:%SZ" and calls[1] == "git fetch -q origin main"
    assert calls[2] == ("git log --first-parent --diff-filter=A --format=%cI -1 origin/main -- "
                        f".github/workflows/{CHECKS[trigger][0]}")
    assert calls[3] == "date -u -d 2026-11-21T09:00:00+00:00 + 30 minutes +%Y-%m-%dT%H:%M:%SZ"
    assert "--until 2026-11-21T18:47:03Z" in calls[4] and "--since 2026-11-21T09:30:00Z" in calls[4]
    assert f"--trigger {trigger}" in calls[4]
    if label:
        assert label in r.stdout and written == label + "\n"        # what the email's subject carries
    else:
        assert "TRIGGER" not in r.stdout and "CANNOT JUDGE (not a trigger):" not in r.stdout and written is None


@pytest.mark.parametrize("trigger", sorted(CHECKS))
def test_without_the_merge_on_mains_line_the_step_cannot_judge_and_runs_nothing(tmp_path, trigger):
    r, calls, written = _run_step(tmp_path, trigger, 0, ["x"], merged="")
    assert r.returncode == 2
    assert "CANNOT JUDGE (not a trigger): the merge that added this workflow is not on main's line" in r.stdout
    assert written.startswith("CANNOT JUDGE (not a trigger): the merge") and not any(c.startswith("uv") for c in calls)


def test_the_start_is_found_at_the_merge_that_added_the_workflow(tmp_path):
    """The step's own git question on a real line: the merge commit (main's
    first parent line, the file added relative to its first parent), not the
    branch commit that wrote the file."""
    r = tmp_path / "line"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "made up")
    _git(r, "config", "user.email", "made-up@example.test")
    (r / "README").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "start")
    _git(r, "switch", "-qc", "side")
    (r / ".github/workflows").mkdir(parents=True)
    (r / ".github/workflows/write-gap.yml").write_text("name: write-gap\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "add")
    _git(r, "switch", "-q", "main")
    _git(r, "merge", "-q", "--no-ff", "-m", "merge", "side")
    merge = _git(r, "rev-parse", "HEAD")
    got = _git(r, "log", "--first-parent", "--diff-filter=A", "--format=%H", "-1", "main", "--",
               ".github/workflows/write-gap.yml")
    assert got == merge


# --- the email (scripts/trigger_email.py, C2) ---

_SPEC_E = importlib.util.spec_from_file_location("trigger_email", ROOT / "scripts" / "trigger_email.py")
tem = importlib.util.module_from_spec(_SPEC_E)
_SPEC_E.loader.exec_module(tem)
ENV = {"WORKFLOW": "stamp-margin", "RUN_URL": "https://github.example/run/1", "COMMIT": "abc123"}


def test_the_subject_carries_the_label_the_count_and_the_newest_receipt():
    items = ("2026-11-21 es: TRIGGER T2 2026-11-21 [s v1] …\n2026-11-19 jp: TRIGGER T2 2026-11-19 [s v1] …\n"
             "- es: CANNOT JUDGE (not a trigger): …\n")
    subject, body = tem.compose(items, "TRIGGER T2 (D-36): report to the operator the same day\n", ENV)
    assert subject == "talea stamp-margin RED, TRIGGER T2 (D-36): 3 items, newest receipt 2026-11-21"
    lines = body.splitlines()
    assert lines[0] == "stamp-margin: red. TRIGGER T2 (D-36): report to the operator the same day"
    assert lines[1:3] == ["Run: https://github.example/run/1", "Commit: abc123"]
    assert lines[4:8] == ["What set the verdict, newest first:", *items.splitlines()]
    assert "never reopens D-36" in body


@pytest.mark.parametrize("items, label, subject_end, says", [
    (None, None, "CANNOT JUDGE (not a trigger): no list", "The check wrote no list"),
    ("", "T1 CANNOT JUDGE (not a trigger): detector health; it never reopens D-36\n",
     "T1 CANNOT JUDGE (not a trigger): 0 items", "listed nothing"),
    ("- CANNOT JUDGE (not a trigger): the history\n", "CANNOT JUDGE (not a trigger): detector health\n",
     "CANNOT JUDGE (not a trigger): 1 item", "- CANNOT JUDGE (not a trigger): the history"),
])
def test_without_a_list_or_a_date_the_email_says_so(items, label, subject_end, says):
    subject, body = tem.compose(items, label, ENV)
    assert subject == f"talea stamp-margin RED, {subject_end}" and says in body


class _SMTP:
    sent: list = []
    refuse: dict = {}

    def __init__(self, host, port, timeout):
        assert (host, port) == ("smtp.gmail.com", 587)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        _SMTP.context = context

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        _SMTP.sent.append(msg)
        return dict(_SMTP.refuse)


def _files(tmp_path, items="2026-11-21 es: TRIGGER T2 x\n", label="TRIGGER T2 (D-36): y\n"):
    (tmp_path / "items.txt").write_text(items)
    (tmp_path / "label.txt").write_text(label)
    return [str(tmp_path / "items.txt"), str(tmp_path / "label.txt")]


def test_the_email_goes_from_the_alert_account_with_its_copy(tmp_path, monkeypatch, capsys):
    for k, v in {**ENV, "ALERT_SMTP_USER": "alerts@example.test", "ALERT_SMTP_PASSWORD": "pw",
                 "ALERT_EMAIL_TO": "op@example.test"}.items():
        monkeypatch.setenv(k, v)
    _SMTP.sent, _SMTP.refuse = [], {}
    assert tem.main(_files(tmp_path), _smtp=_SMTP) == 0
    msg = _SMTP.sent[-1]
    assert (msg["From"], msg["To"], msg["Cc"]) == ("alerts@example.test", "op@example.test", "alerts@example.test")
    assert msg["Subject"] == "talea stamp-margin RED, TRIGGER T2 (D-36): 1 item, newest receipt 2026-11-21"
    assert "2026-11-21 es: TRIGGER T2 x" in msg.get_content()
    assert capsys.readouterr().out.splitlines()[-1] == "email sent"
    # ruled (round 1): STARTTLS verifies the server before the credentials go
    import ssl
    ctx = _SMTP.context
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_missing_settings_or_a_refused_operator_fail_and_print_no_address(tmp_path, monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert tem.main(_files(tmp_path), _smtp=_SMTP) == 1
    assert "EMAIL-NOT-CONFIGURED" in capsys.readouterr().err
    for k, v in {"ALERT_SMTP_USER": "alerts@example.test", "ALERT_SMTP_PASSWORD": "pw",
                 "ALERT_EMAIL_TO": "op@example.test"}.items():
        monkeypatch.setenv(k, v)
    _SMTP.refuse = {"op@example.test": (550, b"no")}
    assert tem.main(_files(tmp_path), _smtp=_SMTP) == 1
    err = capsys.readouterr().err
    assert "EMAIL-REFUSED" in err and "[550]" in err and "op@example" not in err
    _SMTP.refuse = {"alerts@example.test": (550, b"no")}
    assert tem.main(_files(tmp_path), _smtp=_SMTP) == 0
    assert "refused the copy only (SMTP codes [550])" in capsys.readouterr().out
    _SMTP.refuse = {}


def test_a_failing_server_exits_1_with_its_type_and_codes_only(tmp_path, monkeypatch, capsys):
    import smtplib
    for k, v in {**ENV, "ALERT_SMTP_USER": "alerts@example.test", "ALERT_SMTP_PASSWORD": "pw",
                 "ALERT_EMAIL_TO": "op@example.test"}.items():
        monkeypatch.setenv(k, v)

    class Down(_SMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"op@example.test rejected")
    def hard_exit(code):
        raise SystemExit(code)
    with pytest.raises(SystemExit) as gone:
        tem.main(_files(tmp_path), _smtp=Down, _exit=hard_exit)
    err = capsys.readouterr().err
    assert gone.value.code == 1 and "EMAIL-FAILED: SMTPAuthenticationError (SMTP codes [535])" in err
    assert "op@example" not in err


def test_the_email_script_uses_the_standard_library_only():
    text = (ROOT / "scripts" / "trigger_email.py").read_text()
    imports = set(re.findall(r"^(?:from|import) ([\w.]+)", text, re.M))
    assert imports <= {"__future__", "os", "re", "smtplib", "ssl", "sys", "email.message", "email.utils", "pathlib"}
