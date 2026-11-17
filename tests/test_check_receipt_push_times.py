"""The push-time detector (register T-35, D-36): did each receipt reach GitHub
before its market's deadline?

A real git repository is built for each test (receipts appended commit by
commit, a pull request merged into main, a branch never merged), and GitHub's
activity record is a fake that answers page by page. The detector must say ON
TIME, UNVERIFIABLE or CANNOT JUDGE, never "leak"; walk only main's first-parent
line; take the earliest push that delivered a commit; refuse to judge from a
history it cannot show complete; and print no price."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import private_file

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_receipt_push_times", ROOT / "scripts" / "check_receipt_push_times.py")
cpt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cpt                 # dataclasses look their module up while it loads
_SPEC.loader.exec_module(cpt)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
ES = SimpleNamespace(deadline_hour=13, tz=ZoneInfo("Europe/Madrid"))      # 12:00Z in winter
MARKETS = {"es": ES}
API = cpt.API


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


def _append(repo: Path, *receipts: dict, slug: str = "es") -> str:
    f = repo / "Data" / slug / "receipts.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as out:
        for r in receipts:
            out.write(json.dumps(r) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"data: tick {receipts[0]['basis_day'] if receipts else ''}")
    return _git(repo, "rev-parse", "HEAD")


def _push(at: str, before: str, after: str) -> dict:
    return {"timestamp": at, "before": before, "after": after, "activity_type": "push",
            "ref": "refs/heads/main"}


def _get(*pages):
    """A fake GitHub: `pages` are lists of activity entries, newest first; each
    page names the next one, the last names none."""
    seen = []
    def get(url, token):
        seen.append((url, token))
        n = len(seen) - 1
        return pages[n], (f"{API}repos/o/r/activity?page={n + 2}" if n + 1 < len(pages) else None)
    get.seen = seen
    return get


def _run(repo, get, capsys, days=7):
    rc = cpt.run(repo, "o/r", "HEAD", NOW, days, MARKETS, "tok", get=get)
    return rc, capsys.readouterr().out


def test_on_time_and_unverifiable_by_githubs_push_time(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    get = _get([_push("2026-11-15T23:00:00Z", a, b),             # b: after its 12:00Z deadline
                _push("2026-11-14T10:01:00Z", start, a)])        # a: on time
    rc, out = _run(repo, get, capsys)
    assert rc == 1
    assert "[push-times] es: ON TIME 1, UNVERIFIABLE 1, CANNOT JUDGE 0" in out
    assert ("es: UNVERIFIABLE 2026-11-16 [battery-2h2h-persistence v1] stamped 2026-11-15T10:00:01+00:00, "
            "pushed 2026-11-15T23:00:00+00:00, deadline 2026-11-15T12:00:00+00:00") in out
    assert "leak" not in out.lower() and "87.65" not in out           # never "leak"; no price
    assert get.seen[0] == (f"{API}repos/o/r/activity?ref=refs/heads/main&per_page=100", "tok")


def test_a_push_exactly_at_the_deadline_is_unverifiable(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    rc, out = _run(repo, _get([_push("2026-11-15T12:00:00Z", start, a)]), capsys)
    assert rc == 1 and "UNVERIFIABLE 1" in out


def test_the_deadline_is_the_markets_own_hour_in_its_own_zone():
    summer = cpt.deadline({"basis_day": "2026-07-01"}, ES)
    winter = cpt.deadline({"basis_day": "2026-12-01"}, ES)
    assert summer == datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)
    assert winter == datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    chicago = SimpleNamespace(deadline_hour=10, tz=ZoneInfo("America/Chicago"))
    assert cpt.deadline({"basis_day": "2026-11-16"}, chicago) == datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)


def test_a_commit_no_push_delivered_cannot_be_judged(repo, capsys):
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    rc, out = _run(repo, _get([]), capsys)
    assert rc == 2
    assert "es: ON TIME 0, UNVERIFIABLE 0, CANNOT JUDGE 1" in out and "pushed not in the record" in out
    assert "ends before it" in out


def test_unverifiable_wins_over_cannot_judge(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))       # never pushed
    rc, out = _run(repo, _get([_push("2026-11-15T23:00:00Z", start, a)]), capsys)
    assert rc == 1 and "UNVERIFIABLE 1, CANNOT JUDGE 1" in out


def test_the_earliest_push_that_delivered_a_commit_counts(repo, capsys):
    """The same commit is in a later push's span too (its before is older):
    the first delivery is the one that counts."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:02+00:00", "battery-2h2h-weekly"))
    rc, out = _run(repo, _get([_push("2026-11-15T20:00:00Z", start, b),
                               _push("2026-11-15T10:05:00Z", start, a)]), capsys)
    assert "es: ON TIME 1, UNVERIFIABLE 1" in out and rc == 1
    assert "battery-2h2h-weekly" in out.split("UNVERIFIABLE 2026")[1]


def test_only_mains_first_parent_line_counts(repo, capsys):
    """A receipt added on a pull request's branch is attributed to the merge
    commit on main, pushed when the merge was; a branch never merged is not
    read at all."""
    start = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "side")
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "never-merged")
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:09+00:00", "battery-2h2h-weekly"))
    _git(repo, "switch", "-q", "main")
    side_commit = _git(repo, "rev-parse", "side")
    rc, out = _run(repo, _get([_push("2026-11-15T10:30:00Z", start, merge),
                               _push("2026-11-15T10:01:00Z", start, side_commit),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert "es: ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 0" in out and rc == 0
    added, _ = cpt.added_receipts(repo, "HEAD", "es", start)
    assert [a.commit for a in added] == [merge]                     # not the branch's own commit
    assert "battery-2h2h-weekly" not in out


def test_a_commit_that_rewrote_the_file_cannot_be_judged(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    f = repo / "Data" / "es" / "receipts.jsonl"
    f.write_text(json.dumps(_receipt("2026-11-15", "2026-11-15T10:00:05+00:00")) + "\n")
    _git(repo, "commit", "-qam", "rewritten")
    b = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, b)]), capsys)
    assert rc == 2 and f"CANNOT JUDGE commit {b[:7]}: the file there does not begin" in out
    assert "ON TIME 1" in out                                         # a, delivered in the same push


def test_a_backdated_stamp_never_hides_a_late_push(repo, capsys):
    """Sonnet, round 1: the window was chosen by the receipt's own stamp, the
    clock this check exists not to trust. A writer whose clock runs days
    behind stamps a receipt "2026-11-01" and pushes it on 11-15, long after
    the deadline its basis day gives: it is judged, UNVERIFIABLE, exit 1,
    with and without a start date after its stamp."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-01", "2026-11-01T10:00:01+00:00"))
    for since in (None, datetime(2026, 11, 10, tzinfo=timezone.utc)):
        get = _get([_push("2026-11-15T10:05:00Z", start, a), _push("2026-11-08T00:00:00Z", "", start)])
        rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=get, start=since)
        out = capsys.readouterr().out
        assert rc == 1 and "es: ON TIME 0, UNVERIFIABLE 1, CANNOT JUDGE 0" in out, out
        assert "before the start date: information" not in out


def test_the_window_is_main_after_its_tip_at_the_windows_start(repo, capsys):
    """What GitHub delivered before the window is not walked, whatever the
    stamps say; what came after is, even with no push in the record."""
    start = _git(repo, "rev-parse", "HEAD")
    old = _append(repo, _receipt("2026-11-01", "2026-11-20T10:00:01+00:00"))     # a stamp in the future
    new = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    unpushed = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:02+00:00", "battery-2h2h-weekly"))
    get = _get([_push("2026-11-15T10:05:00Z", old, new), _push("2026-11-01T10:05:00Z", start, old)])
    rc, out = _run(repo, get, capsys)
    assert "es: ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 1" in out and rc == 2   # old not walked
    assert f"(after {old[:7]})" in out
    assert cpt.window_base([], NOW) is None
    assert cpt.window_base([cpt.Push(NOW - cpt.timedelta(days=9), "", "a" * 40)], NOW - cpt.timedelta(days=7)) == "a" * 40
    assert cpt.window_base([cpt.Push(NOW, cpt.ZERO, "b" * 40)], NOW - cpt.timedelta(days=7)) is None


def test_a_line_that_is_json_but_not_an_object_cannot_be_judged(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    f = repo / "Data" / "es" / "receipts.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("[1]\n" + json.dumps(_receipt("2026-11-15", "2026-11-15T10:00:01+00:00")) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd")
    a = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a)]), capsys)
    assert rc == 2 and "es: ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 1" in out
    assert "a line is not a JSON object" in out and "Traceback" not in out


def test_a_market_with_nothing_in_the_window_says_so(repo, capsys):
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, {"es": ES, "de": ES}, "tok",
                 get=_get([_push("2026-11-01T00:00:00Z", "", _git(repo, "rev-parse", "HEAD"))]))
    out = capsys.readouterr().out
    assert rc == 0 and "de: ON TIME 0, UNVERIFIABLE 0, CANNOT JUDGE 0 (no receipt in the window)" in out


def test_the_history_is_followed_page_by_page_until_past_the_window(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    page1 = [_push(f"2026-11-15T{h:02d}:00:00Z", a, a) for h in range(23, 11, -1)]
    page2 = [_push("2026-11-15T10:01:00Z", start, a), _push("2026-11-01T00:00:00Z", "", "")]
    page3 = [_push("2026-10-01T00:00:00Z", "", "")]
    get = _get(page1, page2, page3)
    rc, out = _run(repo, get, capsys)
    assert rc == 0 and "ON TIME 1" in out and len(get.seen) == 2


@pytest.mark.parametrize("pages, nxt, says", [
    ([[{"timestamp": "2026-11-15T10:00:00Z"}] * 100], None, "a full page of pushes with no next page"),
    ([[_push("2026-11-14T10:00:00Z", "", ""), _push("2026-11-15T10:00:00Z", "", "")]], None, "not newest first"),
    ([[_push("2026-11-15T10:00:00Z", "", "")]], "https://elsewhere.test/next", "next page is not on its API"),
    ([[{"timestamp": "yesterday"}]], None, "cannot be read"),
    ([[{"timestamp": "2026-11-15T10:00:00"}]], None, "cannot be read"),         # no zone
    ([{"not": "a list"}], None, "is not a list"),
])
def test_a_history_that_cannot_show_itself_complete_is_not_judged(repo, capsys, pages, nxt, says):
    def get(url, token):
        return pages[0], nxt
    rc, out = _run(repo, get, capsys)
    assert rc == 2 and "CANNOT JUDGE" in out and says in out


def test_github_unreachable_or_too_many_pages_is_not_judged(repo, capsys):
    def down(url, token):
        raise cpt.urllib.error.URLError("down")
    rc, out = _run(repo, down, capsys)
    assert rc == 2 and "cannot be read from GitHub" in out
    endless = [_push("2026-11-15T10:00:00Z", "", "")]
    rc, out = _run(repo, lambda url, token: (endless, f"{API}next"), capsys)
    assert rc == 2 and f"more than {cpt.MAX_PAGES} pages" in out


def test_an_unreadable_line_or_basis_day_cannot_be_judged(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    f = repo / "Data" / "es" / "receipts.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text('{"broken\n' + json.dumps({"target": "x", "committed_at": "2026-11-15T10:00:00+00:00"}) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd")
    a = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a)]), capsys)
    assert rc == 2 and "CANNOT JUDGE 2" in out and "deadline unknown" in out


def test_a_push_naming_a_commit_this_clone_lacks(repo, capsys):
    """Codex, round 1: inside the window it cannot be passed over (the commits
    it carried would take a later push's time): exit 2. Before the window it
    cannot have delivered anything the walk covers."""
    week_ago = NOW - cpt.timedelta(days=7)
    assert cpt.delivered_by(repo, [cpt.Push(NOW - cpt.timedelta(days=9), "", "f" * 40),
                                   cpt.Push(NOW, "", cpt.ZERO)], week_ago) == {}
    with pytest.raises(cpt.CannotRead):
        cpt.delivered_by(repo, [cpt.Push(NOW, "", "f" * 40)], week_ago)
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a),            # readable, later
                               _push("2026-11-14T10:05:00Z", start, "f" * 40),    # unreadable, earlier
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 2 and "delivered a commit the line being judged does not contain" in out


def test_main_needs_a_repository_and_reads_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(cpt, "run", lambda repo_dir, repo, ref, now, days, markets, token, get=None, start=None,
                        until=None: seen.update(repo=repo, ref=ref, days=days, token=token, markets=markets,
                                                start=start, until=until) or 0)
    monkeypatch.setattr(cpt, "_registry", lambda: MARKETS)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(SystemExit):
        cpt.main([])
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert cpt.main(["--days", "3", "--ref", "origin/main"], now=NOW) == 0
    assert seen == {"repo": "o/r", "ref": "origin/main", "days": 3, "token": "tok", "markets": MARKETS,
                    "start": None, "until": None}
    assert cpt.main(["--until", "2026-11-17T18:23:05Z"], now=NOW) == 0
    assert seen["until"] == datetime(2026, 11, 17, 18, 23, 5, tzinfo=timezone.utc)
    for bad in ("2026-11-17T18:23:05", "soon"):                # no zone, not a time
        with pytest.raises(SystemExit):
            cpt.main(["--until", bad], now=NOW)
    assert cpt.main(["--since", "2026-11-17"], now=NOW) == 0
    assert seen["start"] == datetime(2026, 11, 17, tzinfo=timezone.utc)
    with pytest.raises(SystemExit):
        cpt.main(["--since", "next week"], now=NOW)


def test_the_real_registry_gives_every_market_a_deadline_hour_and_a_zone():
    for slug, m in cpt._registry().items():
        assert isinstance(m.deadline_hour, int) and 0 <= m.deadline_hour < 24, slug
        assert cpt.deadline({"basis_day": "2026-11-16"}, m).tzinfo is not None, slug


def test_the_github_reader_follows_the_link_header(monkeypatch):
    class Resp:
        headers = {"Link": f'<{API}repos/o/r/activity?page=2>; rel="next", <x>; rel="last"'}
        def read(self):
            return b"[]"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    seen = []
    monkeypatch.setattr(cpt.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp())
    body, nxt = cpt._get(f"{API}repos/o/r/activity", "tok")
    assert body == [] and nxt == f"{API}repos/o/r/activity?page=2"
    assert seen[0].get_header("Authorization") == "Bearer tok"


def test_receipts_before_the_start_date_are_information_not_the_verdict(repo, capsys):
    """The backlog case: receipts GitHub received before --since are listed,
    with the same words, but do not set the exit code; one with no push in the
    record always does."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    get = _get([_push("2026-11-15T10:05:00Z", a, b), _push("2026-11-14T23:00:00Z", start, a),
                _push("2026-11-01T00:00:00Z", "", start)])
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=get,
                 start=datetime(2026, 11, 15, tzinfo=timezone.utc))
    out = capsys.readouterr().out
    assert rc == 0
    assert ("es: ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 0; before the start date: "
            "ON TIME 0, UNVERIFIABLE 1, CANNOT JUDGE 0") in out
    assert "UNVERIFIABLE 2026-11-16 [battery-2h2h-persistence v1] stamped 2026-11-14T10:00:01+00:00" in out
    assert "(before the start date: information)" in out
    assert "the verdict covers receipts GitHub received from 2026-11-15T00:00Z" in out
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=_get([]),
                 start=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert rc == 2                                # after the start date, not in the record


def test_a_ref_git_cannot_read_is_not_judged(repo, capsys):
    rc = cpt.run(repo, "o/r", "no-such-ref", NOW, 7, MARKETS, "tok", get=_get([]))
    out = capsys.readouterr().out
    assert rc == 2 and "CANNOT JUDGE: git log failed" in out


def test_a_receipt_without_a_stamp_is_still_judged_by_its_push(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    r = _receipt("2026-11-15", "x")
    del r["committed_at"]
    a = _append(repo, r)
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 0 and "es: ON TIME 1" in out



def test_a_push_history_that_does_not_reach_the_window_is_never_a_pass(repo, capsys):
    """Codex, round 1: everything read is ON TIME, but GitHub's record ends
    inside the window: what came before it is unknown. Exit 2."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a)]), capsys)
    assert rc == 2 and "ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 0" in out
    assert "GitHub's push history ends before the window's start" in out


def test_old_committer_dates_never_hide_a_receipt(repo, capsys):
    """Codex, round 1: nothing is selected by a commit's date (the writer's
    clock). A commit dated 2020 that reached main inside the window is walked."""
    start = _git(repo, "rev-parse", "HEAD")
    f = repo / "Data" / "es" / "receipts.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(_receipt("2026-11-15", "2026-11-15T10:00:01+00:00")) + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "data: tick"], check=True, env={
        "PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"})
    a = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, _get([_push("2026-11-15T23:00:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 1 and "es: ON TIME 0, UNVERIFIABLE 1, CANNOT JUDGE 0" in out


def test_a_file_that_cannot_be_read_is_never_taken_as_empty(repo, capsys):
    """Codex, round 1: a blob missing from the clone used to read as an empty
    file. Absence is only what git confirms; a failed read is exit 2."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    blob = _git(repo, "rev-parse", f"{a}:Data/es/receipts.jsonl")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    rc, out = _run(repo, _get([_push("2026-11-15T10:05:00Z", start, b),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 2 and "CANNOT JUDGE: git could not read Data/es/receipts.jsonl" in out
    assert cpt._blob(repo, start, "Data/es/receipts.jsonl") == b""          # confirmed absent
    with pytest.raises(cpt.CannotRead):
        cpt._blob(repo, "f" * 40, "Data/es/receipts.jsonl")                  # not a commit


def test_a_field_not_in_its_known_shape_prints_invalid(repo, capsys):
    """Codex, round 1: only a date, a strategy name, a version number and a UTC
    time are printed; a price or anything else in a field prints <invalid>."""
    start = _git(repo, "rev-parse", "HEAD")
    odd = dict(_receipt("2026-11-15", "87.6543"), target="87.6543", strategy="87.6543",
               strategy_version="87.6543")
    a = _append(repo, odd)
    rc, out = _run(repo, _get([_push("2026-11-15T23:00:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 1 and "87.65" not in out
    assert "UNVERIFIABLE <invalid> [<invalid> v<invalid>] stamped <invalid>" in out
    good = _receipt("2026-11-15", "2026-11-15T10:00:01.123456+00:00")
    assert [cpt.shown(good, k) for k in ("target", "committed_at")] == [
        "2026-11-16", "2026-11-15T10:00:01.123456+00:00"]
    assert cpt.shown_strategy(good, cpt._strategies()) == ("battery-2h2h-persistence", "1")
    assert cpt.shown({"target": 20261116}, "target") == "<invalid>"


def test_a_run_writes_nothing_in_a_fresh_checkout(tmp_path):
    """Codex, round 1: importing the registry must not write __pycache__ into
    the checkout. A fresh copy (script + package, committed) is clean after
    the registry is loaded the way main() loads it."""
    fresh = tmp_path / "fresh"
    (fresh / "scripts").mkdir(parents=True)
    import shutil
    shutil.copy(ROOT / "scripts" / "check_receipt_push_times.py", fresh / "scripts")
    shutil.copytree(ROOT / "src" / "talea", fresh / "src" / "talea",
                    ignore=shutil.ignore_patterns("__pycache__"))
    _git(fresh, "init", "-q", "-b", "main")
    _git(fresh, "config", "user.name", "made up")
    _git(fresh, "config", "user.email", "made-up@example.test")
    _git(fresh, "add", "-A")
    _git(fresh, "commit", "-qm", "fresh")
    # run as the operator runs it; GitHub is made unreachable (a proxy on a
    # closed local port), so it stops at "cannot be read" after loading the registry
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HTTPS_PROXY": "http://localhost:9",
           "https_proxy": "http://localhost:9", "NO_PROXY": "", "no_proxy": ""}
    r = subprocess.run([sys.executable, "-I", "scripts/check_receipt_push_times.py", "--repo", "o/r"],
                       cwd=fresh, capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2 and "cannot be read from GitHub" in r.stdout, r.stdout + r.stderr
    assert _git(fresh, "status", "--porcelain", "--ignored") == ""
    assert not list(fresh.rglob("__pycache__"))



# --- round 3 (Codex, round 2) ---

def test_a_main_that_was_reset_is_not_judged(repo, capsys):
    """A→B, then main reset to A: B is not on main any more, so what GitHub
    says it delivered cannot be laid on this line. Exit 2."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    _git(repo, "reset", "-q", "--hard", a)
    rc, out = _run(repo, _get([_push("2026-11-15T12:00:00Z", b, a), _push("2026-11-15T10:05:00Z", a, b),
                               _push("2026-11-14T10:05:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 2 and "delivered a commit the line being judged does not contain" in out


def test_a_stale_local_ref_is_not_judged(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    get = _get([_push("2026-11-15T10:05:00Z", a, b), _push("2026-11-14T10:05:00Z", start, a),
                _push("2026-11-01T00:00:00Z", "", start)])
    rc = cpt.run(repo, "o/r", a, NOW, 7, MARKETS, "tok", get=get)       # the ref is behind GitHub's main
    out = capsys.readouterr().out
    assert rc == 2 and "delivered a commit the line being judged does not contain" in out


def test_a_shallow_clone_is_not_judged(repo, tmp_path, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True,
                   capture_output=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    rc, out = _run(shallow, _get([_push("2026-11-15T10:05:00Z", start, a),
                                  _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 2 and "CANNOT JUDGE: this clone is shallow" in out


def test_every_git_call_forbids_lazy_fetch_and_prompts(repo, monkeypatch, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    real, envs = subprocess.run, []
    def spy(cmd, **kw):
        if cmd[0] == "git":
            envs.append(kw.get("env") or {})
        return real(cmd, **kw)
    monkeypatch.setattr(cpt.subprocess, "run", spy)
    _run(repo, _get([_push("2026-11-15T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert len(envs) >= 5
    assert all(e.get("GIT_NO_LAZY_FETCH") == "1" and e.get("GIT_TERMINAL_PROMPT") == "0" for e in envs)
    code = (ROOT / "scripts" / "check_receipt_push_times.py").read_text()
    assert code.count("subprocess.run(") == 1                   # the one runner every git call goes through


def test_git_failures_print_fixed_words_never_gits_own_output(repo, capsys):
    rc = cpt.run(repo, "o/r", "no-such-ref", NOW, 7, MARKETS, "tok",
                 get=_get([_push("2026-11-01T00:00:00Z", "", _git(repo, "rev-parse", "HEAD"))]))
    out = capsys.readouterr().out
    assert rc == 2 and "CANNOT JUDGE: git log failed" in out
    assert "fatal" not in out and "no-such-ref'" not in out and "ambiguous" not in out


def test_only_a_registered_strategy_and_version_are_printed(repo, capsys):
    """Codex, round 2: a shape check let "leak" through as a strategy name and
    any digits as a version. Only the pairs registered in the code print."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, dict(_receipt("2026-11-15", "2026-11-15T10:00:01+00:00"), strategy="leak"),
                dict(_receipt("2026-11-15", "2026-11-15T10:00:02+00:00"), strategy_version="8765"))
    rc, out = _run(repo, _get([_push("2026-11-15T23:00:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 1 and "leak" not in out.lower() and "8765" not in out
    assert out.count("[<invalid> v<invalid>]") == 2
    assert cpt.shown_strategy({"strategy": "battery-2h2h-weekly", "strategy_version": "1"},
                              cpt._strategies()) == ("battery-2h2h-weekly", "1")


def test_the_days_option_says_what_it_counts(capsys):
    with pytest.raises(SystemExit):
        cpt.main(["--help"])
    assert "receipts on commits GitHub received in the last N days" in " ".join(capsys.readouterr().out.split())



# --- round 4 (Codex, round 3) ---

def test_a_partial_clone_is_refused_before_any_object_is_read(repo, capsys):
    """Whatever the git version: a clone that may fetch missing objects from a
    promisor remote is not judged offline. Exit 2, before any object read."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    pages = [_push("2026-11-15T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]
    for key, value, says in (("remote.origin.promisor", "true", "promisor remote configured: cannot judge offline"),
                             ("extensions.partialClone", "origin", "partial clone: cannot judge offline")):
        _git(repo, "config", key, value)
        rc, out = _run(repo, _get(pages), capsys)
        assert rc == 2 and f"CANNOT JUDGE: {says}" in out
        assert "ON TIME" not in out
        _git(repo, "config", "--unset", key)
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 0 and "es: ON TIME 1" in out                  # neither key: judged


def test_the_ancestry_failure_never_prints_the_ref(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    _git(repo, "branch", "leak", a)
    get = _get([_push("2026-11-15T10:05:00Z", a, b), _push("2026-11-14T10:05:00Z", start, a),
                _push("2026-11-01T00:00:00Z", "", start)])
    rc = cpt.run(repo, "o/r", "leak", NOW, 7, MARKETS, "tok", get=get)
    out = capsys.readouterr().out
    assert rc == 2 and "delivered a commit the line being judged does not contain" in out
    assert "leak" not in out.lower()


def test_a_strategy_or_version_that_is_not_a_string_prints_invalid(repo, capsys):
    pairs = cpt._strategies()
    for odd in ({"strategy": ["battery-2h2h-weekly"], "strategy_version": "1"},
                {"strategy": "battery-2h2h-weekly", "strategy_version": {"v": 1}},
                {"strategy": "battery-2h2h-weekly", "strategy_version": 1}):
        assert cpt.shown_strategy(odd, pairs) == ("<invalid>", "<invalid>")
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, dict(_receipt("2026-11-15", "2026-11-15T10:00:01+00:00"), strategy=[87.65], strategy_version={"x": 1}))
    rc, out = _run(repo, _get([_push("2026-11-15T23:00:00Z", start, a),
                               _push("2026-11-01T00:00:00Z", "", start)]), capsys)
    assert rc == 1 and "[<invalid> v<invalid>]" in out and "87.65" not in out and "Traceback" not in out



# --- rounds 5 to 7 (Codex and Sonnet, rounds 4 to 6): no config value is parsed ---

def _a_judgeable_repo(repo):
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    return [_push("2026-11-15T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]


NAMES = ("o", "a\u00a0b", "a b", "a\u2003b.c", "a\rb")


def test_any_promisor_key_is_refused_whatever_its_value_and_its_remotes_name(repo, monkeypatch, capsys):
    """Round 7: parsing the value was a source of escapes (a bare key, Unicode
    spaces, a carriage return in a remote's name). Now any remote.<name>.promisor
    key, whatever its value (false included) and whatever the name, is refused;
    and no git command that reads objects runs before the refusal."""
    pages = _a_judgeable_repo(repo)
    config = repo / ".git" / "config"
    plain = config.read_text()
    for value in ("", " = 2", " = yes", " = false", " = maybe"):
        config.write_text(plain + f'[remote "o"]\n\tpromisor{value}\n')
        rc, out = _run(repo, _get(pages), capsys)
        assert rc == 2 and "CANNOT JUDGE: promisor remote configured: cannot judge offline" in out, value
    config.write_text(plain)
    for name in NAMES:
        for value in ("true", "false"):
            _git(repo, "config", f"remote.{name}.promisor", value)
            real, seen = cpt._gitrun, []
            def guard(r, *args, text=True):
                seen.append(args[0])
                assert args[0] not in ("log", "show", "rev-list", "cat-file", "merge-base", "ls-tree")
                return real(r, *args, text=text)
            monkeypatch.setattr(cpt, "_gitrun", guard)
            rc, out = _run(repo, _get(pages), capsys)
            monkeypatch.setattr(cpt, "_gitrun", real)
            assert rc == 2 and "promisor remote configured: cannot judge offline" in out, (repr(name), value)
            assert set(seen) <= {"config"}
            _git(repo, "config", "--unset", f"remote.{name}.promisor")
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 0 and "es: ON TIME 1" in out


def test_a_partial_clone_extension_is_refused_whatever_its_value(repo, capsys):
    pages = _a_judgeable_repo(repo)
    config = repo / ".git" / "config"
    plain = config.read_text()
    config.write_text(plain + "[extensions]\n\tpartialClone = o\n")
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 2 and "partial clone: cannot judge offline" in out
    config.write_text(plain + "[extensions]\n\tpartialClone\n")      # bare: git exits 128
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 2 and "git config cannot be read, so a partial clone cannot be ruled out" in out
    config.write_text(plain)


def test_a_config_query_that_fails_is_never_taken_as_none(repo, monkeypatch, capsys):
    pages = _a_judgeable_repo(repo)
    real = cpt._gitrun
    for which in ("--get", "--get-regexp"):                   # any exit but 0 or 1, for both queries
        def failing(r, *args, text=True, which=which):
            if args[0] == "config" and which in args:
                return cpt.subprocess.CompletedProcess(args, 2, stdout="", stderr="")
            return real(r, *args, text=text)
        monkeypatch.setattr(cpt, "_gitrun", failing)
        rc, out = _run(repo, _get(pages), capsys)
        monkeypatch.setattr(cpt, "_gitrun", real)
        assert rc == 2 and "git config cannot be read" in out, which
    def empty_but_found(r, *args, text=True):                 # exit 0 with no output: still a key
        if args[:2] == ("config", "--get-regexp"):
            return cpt.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real(r, *args, text=text)
    monkeypatch.setattr(cpt, "_gitrun", empty_but_found)
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 2 and "promisor remote configured" in out



# --- the daily workflow's race, and more config cases ---

def test_with_until_main_is_judged_as_github_recorded_it_then(repo, capsys):
    """The workflow notes the time, then fetches, then reads GitHub's record.
    With --until, main is judged as GitHub recorded it then, and FAILS CLOSED
    on what was cut off: a commit after that tip is left for the next run only
    if a push after --until delivered it."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:02+00:00", "battery-2h2h-weekly"))
    noted = datetime(2026, 11, 15, 10, 6, tzinfo=timezone.utc)
    older = [_push("2026-11-15T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]
    # a later push delivered b: b is left for the next run, the rest is judged
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok",
                 get=_get([_push("2026-11-15T10:20:00Z", b, ""),            # an event with no commit: passed over
                           _push("2026-11-15T10:10:00Z", a, b)] + older), until=noted)
    out = capsys.readouterr().out
    assert rc == 0 and "es: ON TIME 1, UNVERIFIABLE 0, CANNOT JUDGE 0" in out, out
    # the clone holds b, and no push after --until is shown to have delivered it: not a pass
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok",
                 get=_get([_push("2026-11-15T10:10:00Z", a, "f" * 40)] + older), until=noted)
    out = capsys.readouterr().out
    assert rc == 2 and "1 commit(s) after main's tip at --until: delivery by a push after it could not be verified" in out
    # nothing pushed after --until and nothing cut off: judged
    rc = cpt.run(repo, "o/r", a, NOW, 7, MARKETS, "tok",
                 get=_get([_push("2026-11-15T10:10:00Z", a, "f" * 40)] + older), until=noted)
    assert rc == 0 and "es: ON TIME 1" in capsys.readouterr().out
    # without --until, a push the clone does not hold is exit 2
    rc, out = _run(repo, _get([_push("2026-11-15T10:10:00Z", a, "f" * 40)] + older), capsys)
    assert rc == 2 and "does not contain" in out
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=_get([_push("2026-11-01T00:00:00Z", "", start)]),
                 until=noted)
    assert rc == 2                      # no push after --until at all: a and b on the line, delivery unverified


def test_a_later_push_the_clone_cannot_read_verifies_nothing(repo, tmp_path, capsys):
    """Codex, round 2: a REAL A→B→C. The clone holds B but not C; GitHub's record
    shows one push A→C after --until. That push may well have delivered B, but
    the clone cannot read its range, so B's delivery cannot be verified: exit
    2, never "delivered by no push"."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:02+00:00", "battery-2h2h-weekly"))   # B
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull})
    for k, v in (("user.name", "made up"), ("user.email", "made-up@example.test")):
        _git(other, "config", k, v)
    c = _append(other, _receipt("2026-11-15", "2026-11-15T10:00:03+00:00", "battery-2h2h-rankblend"))  # C, elsewhere
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", c]).returncode != 0         # the clone lacks C
    pages = [_push("2026-11-15T10:10:00Z", a, c),                                               # A→C after --until
             _push("2026-11-15T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=_get(pages),
                 until=datetime(2026, 11, 15, 10, 6, tzinfo=timezone.utc))
    out = capsys.readouterr().out
    assert rc == 2 and "1 commit(s) after main's tip at --until: delivery by a push after it could not be verified" in out
    assert "no push" not in out


def test_a_reset_before_until_restored_after_it_is_not_judged(repo, capsys):
    """Codex, round 1: A→B, then main reset B→A (both before --until), then
    A→B again after it. The tip at --until is A, but a push before --until
    delivered B, which A does not contain. Exit 2, not a judgement of A."""
    start = _git(repo, "rev-parse", "HEAD")
    a = _append(repo, _receipt("2026-11-14", "2026-11-14T10:00:01+00:00"))
    b = _append(repo, _receipt("2026-11-15", "2026-11-15T10:00:01+00:00"))
    pages = [_push("2026-11-15T12:00:00Z", a, b),                     # after --until: restored
             _push("2026-11-15T11:00:00Z", b, a),                     # before --until: the reset
             _push("2026-11-15T10:05:00Z", a, b),
             _push("2026-11-14T10:05:00Z", start, a), _push("2026-11-01T00:00:00Z", "", start)]
    rc = cpt.run(repo, "o/r", "HEAD", NOW, 7, MARKETS, "tok", get=_get(pages),
                 until=datetime(2026, 11, 15, 11, 30, tzinfo=timezone.utc))
    out = capsys.readouterr().out
    assert rc == 2 and "the push at 2026-11-15T10:05:00Z delivered a commit the line being judged does not contain" in out


def test_promisor_keys_in_any_case_from_an_include_or_a_worktree_are_refused(repo, tmp_path, capsys):
    """The readers' suggestions on the detector: git matches config names in any
    case, follows includes and reads worktree config; the refusal must too."""
    pages = _a_judgeable_repo(repo)
    config = repo / ".git" / "config"
    plain = config.read_text()
    included = tmp_path / "included.cfg"
    included.write_text('[remote "o"]\n\tpromisor = true\n')
    for extra in ('[remote "o"]\n\tPROMISOR = true\n', '[Remote "o"]\n\tPromisor\n',
                  f"[include]\n\tpath = {included}\n"):
        config.write_text(plain + extra)
        rc, out = _run(repo, _get(pages), capsys)
        assert rc == 2 and "promisor remote configured: cannot judge offline" in out, extra
    config.write_text(plain + "[extensions]\n\tworktreeConfig = true\n")
    (repo / ".git" / "config.worktree").write_text('[remote "o"]\n\tpromisor = true\n')
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 2 and "promisor remote configured: cannot judge offline" in out
    (repo / ".git" / "config.worktree").unlink()
    config.write_text(plain)
    rc, out = _run(repo, _get(pages), capsys)
    assert rc == 0


# --- the daily workflow (register T-35) ---

def _workflow() -> str:
    return private_file(".github/workflows/receipt-push-times.yml").read_text()


def _step_script() -> str:
    text = _workflow()
    block = text.split("        run: |\n", 1)[1]
    return "\n".join(l[10:] for l in block.splitlines() if l.startswith("          "))


def test_the_workflow_is_read_only_daily_and_runs_the_check_as_tested():
    text = _workflow()
    assert re.search(r"^permissions:\n  contents: read\n\n", text, re.M)
    assert "write" not in text.split("permissions:", 1)[1].split("jobs:", 1)[0]
    assert re.findall(r'- cron:\s*"([^"]+)"', text) == ["23 18 * * *"] and "workflow_dispatch:" in text
    assert "fetch-depth: 0" in text                                     # never a shallow clone
    script = _step_script()
    assert ('uv run --no-project python -I scripts/check_receipt_push_times.py \\\n'
            '  --repo "$GITHUB_REPOSITORY" --ref origin/main --days 7 --since 2026-11-11 --until "$until" '
            '2>&1 | tee "$log"') in script
    assert script.index('until="$(date') < script.index("git fetch")    # noted BEFORE the fetch
    assert "rolls past this" in text and "2026-11-18" in text            # the start date's reach, said


def _run_step(tmp_path, check_exit: int, last_line: str | None):
    """The step's script, as GitHub runs it (bash -eo pipefail), with stand-ins
    for date, git and uv (uv prints `last_line`, if any, and exits
    `check_exit`), recording the order they were called in."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    say = f'echo "{last_line}"; ' if last_line else ""
    for name, body in (("date", 'echo "date $*" >> "$LOG"; echo 2026-11-17T18:23:03Z'),
                       ("git", 'echo "git $*" >> "$LOG"'),
                       ("uv", f'echo "uv $*" >> "$LOG"; echo "[push-times] es: ..."; {say}exit {check_exit}')):
        (bin_ / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_ / name).chmod(0o755)
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _step_script()],
                       capture_output=True, text=True, env={"PATH": f"{bin_}:/usr/bin:/bin", "LOG": str(log),
                                                            "GITHUB_REPOSITORY": "o/r", "RUNNER_TEMP": str(tmp_path)})
    return r, log.read_text().splitlines()


STEP_CASES = [  # (the check's exit, its last line, the step's exit, the trigger printed)
    (0, "[push-times] result: ON TIME", 0, None),
    (1, "[push-times] result: UNVERIFIABLE", 1, "TRIGGER T3 (UNVERIFIABLE)"),
    (2, "[push-times] result: CANNOT JUDGE", 2, "TRIGGER T4 (CANNOT JUDGE)"),
    (1, None, 2, "TRIGGER T4 (CANNOT JUDGE)"),          # uv failed, or the check never finished
    (0, None, 2, "TRIGGER T4 (CANNOT JUDGE)"),          # a 0 without its line: fail closed
    (1, "[push-times] result: ON TIME", 2, "TRIGGER T4 (CANNOT JUDGE)"),
]


@pytest.mark.parametrize("check_exit, last, step_exit, trigger", STEP_CASES)
def test_the_step_notes_the_time_before_fetching_and_trusts_only_the_checks_last_line(
        tmp_path, check_exit, last, step_exit, trigger):
    """Sonnet, round 1: Python's own exit for a crash, and uv's for a failure,
    can be 1; T3 must mean UNVERIFIABLE and nothing else."""
    r, calls = _run_step(tmp_path, check_exit, last)
    assert r.returncode == step_exit, (r.stdout, r.stderr)
    assert [c.split()[0] for c in calls] == ["date", "git", "uv"]
    assert calls[0] == "date -u -d 2 seconds ago +%Y-%m-%dT%H:%M:%SZ" and calls[1] == "git fetch -q origin main"
    assert "--until 2026-11-17T18:23:03Z" in calls[2] and "--since 2026-11-11" in calls[2]
    if trigger:
        assert trigger in r.stdout
    else:
        assert "TRIGGER" not in r.stdout


def test_entry_prints_the_result_line_and_never_exits_1_for_a_crash(monkeypatch, capsys):
    for rc, word in ((0, "ON TIME"), (1, "UNVERIFIABLE"), (2, "CANNOT JUDGE")):
        monkeypatch.setattr(cpt, "main", lambda argv=None, rc=rc: rc)
        assert cpt.entry([]) == rc
        assert capsys.readouterr().out.splitlines()[-1] == f"[push-times] result: {word}"
    def crash(argv=None):
        raise KeyError("x")
    monkeypatch.setattr(cpt, "main", crash)
    assert cpt.entry([]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["[push-times] CANNOT JUDGE: unexpected error (KeyError)", "[push-times] result: CANNOT JUDGE"]
    def refuse(argv=None):
        raise SystemExit(2)
    monkeypatch.setattr(cpt, "main", refuse)
    with pytest.raises(SystemExit):
        cpt.entry([])
