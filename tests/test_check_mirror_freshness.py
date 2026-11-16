"""The mirror-freshness check (register T-28): a detector that must be able to fail.

It judges GitHub's push times for the public mirror against the server's three
daily windows on Madrid's clock. These tests pin the rule at its edges, on the
clock-change days, on the two real lags of 2026-11-04 and 11-05 (both LATE,
neither STALE), on two windows removed (STALE), the reading of GitHub's answer
(anything unreadable is exit 2, never a pass), and the drill."""
from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import private_file

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_mirror_freshness", ROOT / "scripts" / "check_mirror_freshness.py")
cmf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cmf)

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc


def _z(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _m(s: str) -> datetime:
    """A Madrid wall-clock time, as a UTC instant."""
    return datetime.fromisoformat(s).replace(tzinfo=MADRID).astimezone(UTC)


def _publishes(first_day: str, until: datetime, lag=timedelta(minutes=1, seconds=6)) -> list[datetime]:
    """A push 1.1 minutes after every window (the measured median) from
    first_day up to `until`, newest first."""
    d0 = datetime.fromisoformat(first_day)
    out = [datetime(d.year, d.month, d.day, hh, mm, tzinfo=MADRID).astimezone(UTC) + lag
           for d in (d0 + timedelta(days=k) for k in range(40)) for hh, mm in cmf.WINDOWS]
    return sorted((p for p in out if p <= until), reverse=True)


# --- the windows ---

def test_the_windows_are_the_calendars():
    """docs/calendar.md section 2 states the server's windows; a change there
    that is not made here (or here and not there) fails."""
    text = private_file("docs/calendar.md").read_text()
    stated = re.search(r"\*\*(\d\d:\d\d), (\d\d:\d\d) and (\d\d:\d\d) every day;", text).groups()
    assert tuple(f"{h:02d}:{m:02d}" for h, m in cmf.WINDOWS) == stated
    assert (cmf.GRACE, cmf.EARLY) == (timedelta(minutes=60), timedelta(minutes=5))


def test_due_windows_are_newest_first_and_only_once_their_grace_has_passed():
    now = _z("2026-11-16T12:30:00")                    # winter: 12:30 Madrid = 11:30Z, due exactly now
    w = cmf.due_windows(now)
    assert w[:4] == [_z("2026-11-16T11:30:00"), _z("2026-11-16T10:00:00"),
                     _z("2026-11-15T16:00:00"), _z("2026-11-15T11:30:00")]
    assert cmf.due_windows(now - timedelta(seconds=1))[0] == _z("2026-11-16T10:00:00")
    assert len(w) == 3 * cmf.LOOKBACK_DAYS + 2         # today's 17:00 is not due yet


@pytest.mark.parametrize("day, offset", [("2027-03-27", 1), ("2027-03-28", 2),
                                         ("2027-10-30", 2), ("2027-10-31", 1)])
def test_the_windows_stay_on_madrids_clock_across_the_clock_changes(day, offset):
    """2027-03-28 and 2027-10-31 are the clock-change days: the windows keep
    their Madrid times, so their UTC instants move by an hour."""
    late_evening = datetime.fromisoformat(f"{day}T23:00:00").replace(tzinfo=MADRID).astimezone(UTC)
    w = sorted(x for x in cmf.due_windows(late_evening) if x.astimezone(MADRID).date().isoformat() == day)
    assert [x.astimezone(MADRID).strftime("%H:%M") for x in w] == ["11:00", "12:30", "17:00"]
    assert [x.hour for x in w] == [11 - offset, 12 - offset, 17 - offset]


# --- the rule ---

def test_missed_at_its_edges():
    w = _z("2026-11-16T10:00:00")
    for p, is_missed in [(w - cmf.EARLY, False), (w - cmf.EARLY - timedelta(seconds=1), True),
                         (w + cmf.GRACE, False), (w + cmf.GRACE + timedelta(seconds=1), True),
                         (w + timedelta(minutes=1), False)]:
        assert cmf.missed(w, [p]) is is_missed, p
    assert cmf.missed(w, [])


def test_every_window_published_is_fresh():
    for now in (_m("2026-11-16T12:00:00"), _m("2026-11-16T13:30:00"), _m("2026-11-16T10:59:00"),
                _m("2026-11-16T23:00:00")):
        pushes = _publishes("2026-11-01", now)
        state, w, newest = cmf.judge(pushes, now)
        assert state == "fresh", now
        assert cmf.late_windows(pushes, now) == []


def test_one_missed_window_is_late_and_two_in_a_row_are_stale():
    pushes = [p for p in _publishes("2026-11-01", _m("2026-11-16T13:30:00"))
              if not _m("2026-11-16T12:30:00") < p < _m("2026-11-16T13:00:00")]
    assert cmf.judge(pushes, _m("2026-11-16T13:30:00"))[0] == "late"   # 12:30 missed, 11:00 held
    two = [p for p in pushes if not _m("2026-11-16T11:00:00") < p < _m("2026-11-16T11:30:00")]
    state, w, newest = cmf.judge(two, _m("2026-11-16T13:30:00"))
    assert (state, w) == ("stale", _m("2026-11-16T12:30:00"))
    assert newest == _m("2026-11-15T17:00:00") + timedelta(minutes=1, seconds=6)
    assert cmf.judge(two, _m("2026-11-16T13:29:59"))[0] == "late"      # 12:30's grace not over yet
    # a push after the earlier window, even outside its grace, means the publisher is alive
    assert cmf.judge(two + [_m("2026-11-16T12:10:00")], _m("2026-11-16T13:30:00"))[0] == "late"
    assert cmf.judge([], _m("2026-11-16T13:30:00"))[0] == "stale"


def test_the_two_real_lags_are_late_never_stale():
    """2026-11-04: the 10:00Z window's push came at 11:31Z (about 90 minutes).
    2026-11-05: the 16:00Z window's push came on 11-06 at 10:01Z, inside the next
    morning's window. Each is one missed window: LATE, and listed for the week."""
    base = [p for p in _publishes("2026-10-25", _z("2026-11-07T23:00:00"))
            if not (_z("2026-11-04T10:00:00") < p < _z("2026-11-04T10:30:00")
                    or _z("2026-11-05T16:00:00") < p < _z("2026-11-06T10:30:00"))]
    pushes = sorted(base + [_z("2026-11-04T11:31:00"), _z("2026-11-06T10:01:00")], reverse=True)
    t = _z("2026-11-01T12:00:00")
    states = set()
    while t < _z("2026-11-07T23:00:00"):
        states.add(cmf.judge([p for p in pushes if p <= t], t)[0])
        t += timedelta(minutes=15)
    assert states == {"fresh", "late"}
    upto = lambda t: [p for p in pushes if p <= t]
    assert cmf.judge(upto(_z("2026-11-05T17:00:00")), _z("2026-11-05T17:00:00"))[0] == "late"
    assert cmf.judge(upto(_z("2026-11-06T09:59:00")), _z("2026-11-06T09:59:00"))[0] == "late"
    assert cmf.judge(upto(_z("2026-11-04T11:00:00")), _z("2026-11-04T11:00:00"))[0] == "late"
    assert cmf.late_windows(pushes, _z("2026-11-07T23:00:00")) == [_z("2026-11-04T10:00:00"),
                                                                   _z("2026-11-05T16:00:00")]


def test_late_windows_cover_seven_days_and_no_more():
    now = _m("2026-11-16T23:00:00")
    lw = cmf.late_windows([], now)
    assert lw == sorted(lw)
    assert min(lw) >= now - timedelta(days=7) and len(lw) == 21
    assert max(lw) == _m("2026-11-16T17:00:00")
    w = _m("2026-11-09T17:00:00")                       # exactly seven days old: still listed
    assert w in cmf.late_windows([], w + timedelta(days=7))
    assert w not in cmf.late_windows([], w + timedelta(days=7, seconds=1))


# --- reading GitHub ---

def _item(ts, kind="push", ref="refs/heads/main"):
    return {"activity_type": kind, "ref": ref, "timestamp": ts}


def _http(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "no", {}, io.BytesIO(b""))


SINCE = _z("2026-11-09T00:00:00")


def _one_page(items):
    return lambda url, token: (items, None)


def test_pushes_to_main_only_are_read():
    seen = []

    def get(url, token):
        seen.append((url, token))
        return [_item("2026-11-16T10:03:00Z", ref="refs/heads/other"),
                _item("2026-11-16T10:02:00Z", kind="force_push"), _item("2026-11-16T10:01:06Z")], None
    assert cmf.mirror_pushes("o/r", "tok", SINCE, get=get) == [_z("2026-11-16T10:01:06")]
    (url, token), = seen
    assert url.startswith("https://api.github.com/repos/o/r/activity?")
    assert "ref=refs/heads/main" in url and "activity_type=push" in url and token == "tok"


def _pages(n_first=100):
    """Page 1: a full page of pushes, all newer than SINCE; page 2: the
    publishes that matter, reaching back past SINCE."""
    p1 = [_item(f"2026-11-16T{10 + k // 60:02d}:{k % 60:02d}:30Z") for k in range(n_first)][::-1]
    p2 = [_item("2026-11-12T10:01:06Z"), _item("2026-11-08T16:01:06Z")]
    return {"https://api.github.com/p1": (p1, "https://api.github.com/p2"),
            "https://api.github.com/p2": (p2, "https://api.github.com/p3")}


def test_pages_are_followed_until_the_week_is_covered():
    """A full first page must not hide the publishes on page 2: they are read,
    and the reading stops at the first page that reaches back past `since`
    (page 3 is never asked for)."""
    pages, asked = _pages(), []

    def get(url, token):
        asked.append(url)
        return pages["https://api.github.com/p1" if "activity" in url else url]
    got = cmf.mirror_pushes("o/r", "tok", SINCE, get=get)
    assert len(got) == 102 and _z("2026-11-12T10:01:06") in got
    assert asked[1:] == ["https://api.github.com/p2"] and len(asked) == 2


def test_a_page_that_ends_exactly_at_since_is_not_yet_past_it():
    """More pushes of that same second may be on the next page."""
    pages = {"p1": ([_item("2026-11-16T10:01:06Z"), _item("2026-11-09T00:00:00Z")], "https://api.github.com/p2"),
             "p2": ([_item("2026-11-09T00:00:00Z"), _item("2026-11-08T16:01:06Z")], None)}
    got = cmf.mirror_pushes("o/r", None, SINCE, get=lambda url, tok: pages["p2" if "p2" in url else "p1"])
    assert got.count(SINCE) == 2


def test_the_end_of_the_history_is_complete():
    """No next page: GitHub has nothing older, so what was read is all there is."""
    assert cmf.mirror_pushes("o/r", None, SINCE, get=_one_page([_item("2026-11-16T10:01:06Z")])) \
        == [_z("2026-11-16T10:01:06")]
    assert cmf.mirror_pushes("o/r", None, SINCE, get=_one_page([])) == []


def test_a_full_page_with_no_next_page_is_not_the_end_of_the_history():
    full = [_item(f"2026-11-16T10:{k // 60:02d}:{k % 60:02d}Z") for k in range(100)][::-1]
    with pytest.raises(cmf.CannotJudge, match="full page"):
        cmf.mirror_pushes("o/r", None, SINCE, get=_one_page(full))
    assert len(cmf.mirror_pushes("o/r", None, SINCE, get=_one_page(full[:99]))) == 99
    reaching_back = full[:99] + [_item("2026-11-08T16:01:06Z")]      # full, but past SINCE: complete
    assert len(cmf.mirror_pushes("o/r", None, SINCE, get=_one_page(reaching_back))) == 100


def test_completeness_that_cannot_be_shown_cannot_be_judged(monkeypatch):
    endless = lambda url, token: ([_item("2026-11-16T10:01:06Z")], "https://api.github.com/more")
    with pytest.raises(cmf.CannotJudge, match="pages"):
        cmf.mirror_pushes("o/r", None, SINCE, get=endless)
    elsewhere = lambda url, token: ([_item("2026-11-16T10:01:06Z")], "https://example.test/next")
    with pytest.raises(cmf.CannotJudge, match="API"):
        cmf.mirror_pushes("o/r", None, SINCE, get=elsewhere)
    disorder = [_item("2026-11-16T10:01:06Z"), _item("2026-11-16T11:31:06Z"), _item("2026-11-01T10:00:00Z")]
    with pytest.raises(cmf.CannotJudge, match="newest first"):
        cmf.mirror_pushes("o/r", None, SINCE, get=_one_page(disorder))
    pages = _pages()
    pages["https://api.github.com/p2"] = ([_item("2026-11-16T12:00:00Z")], None)   # older page, newer push
    with pytest.raises(cmf.CannotJudge, match="newest first"):
        cmf.mirror_pushes("o/r", None, SINCE,
                          get=lambda url, tok: pages["https://api.github.com/p1" if "activity" in url else url])


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_refused_token_is_retried_anonymously(code):
    calls = []

    def get(url, token):
        calls.append(token)
        if token:
            raise _http(code)
        return [_item("2026-11-16T10:01:06Z")], None
    assert cmf.mirror_pushes("o/r", "tok", SINCE, get=get) == [_z("2026-11-16T10:01:06")]
    assert calls == ["tok", None]


def test_after_the_retry_every_page_is_read_without_the_token_and_a_later_refusal_is_final():
    pages, calls = _pages(), []

    def get(url, token):
        calls.append(token)
        if token:
            raise _http(403)
        return pages["https://api.github.com/p1" if "activity" in url else url]
    assert len(cmf.mirror_pushes("o/r", "tok", SINCE, get=get)) == 102
    assert calls == ["tok", None, None]
    calls.clear()

    def refused_later(url, token):
        calls.append(token)
        if "p2" in url:
            raise _http(403)
        return pages["https://api.github.com/p1"]
    with pytest.raises(cmf.CannotJudge):
        cmf.mirror_pushes("o/r", "tok", SINCE, get=refused_later)
    assert calls == ["tok", "tok"]


def test_without_a_token_a_refusal_is_not_retried():
    calls = []

    def get(url, token):
        calls.append(token)
        raise _http(403)
    with pytest.raises(cmf.CannotJudge):
        cmf.mirror_pushes("o/r", None, SINCE, get=get)
    assert calls == [None]


@pytest.mark.parametrize("raised, token", [(_http(403), None), (_http(500), "tok"),
                                           (urllib.error.URLError("down"), "tok"),
                                           (TimeoutError(), "tok"), (ValueError("bad json"), None)])
def test_an_unreadable_history_cannot_be_judged(raised, token):
    def get(url, tok):
        raise raised
    with pytest.raises(cmf.CannotJudge):
        cmf.mirror_pushes("o/r", token, SINCE, get=get)


@pytest.mark.parametrize("answer", [{"message": "x"}, None, [{"ref": "refs/heads/main"}],
                                    [_item("not a time")], [_item("2026-11-16T10:01:06")],
                                    ["a string"], [{"timestamp": "2026-11-16T10:01:06Z"}]])
def test_an_answer_that_is_not_what_it_should_be_cannot_be_judged(answer):
    with pytest.raises(cmf.CannotJudge):
        cmf.mirror_pushes("o/r", None, SINCE, get=_one_page(answer))


def test_the_request_asks_githubs_api_and_reads_the_next_page_from_the_link_header(monkeypatch):
    sent = []

    class Resp:
        def __init__(self, body, link=None):
            self.body, self.headers = body, ({"Link": link} if link else {})
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self.body

    body = b'[{"activity_type": "push", "ref": "refs/heads/main", "timestamp": "2026-11-16T10:01:06Z"}]'
    link = ('<https://api.github.com/repositories/1/activity?after=abc>; rel="next", '
            '<https://api.github.com/repositories/1/activity?before=xyz>; rel="prev"')

    def urlopen(req, timeout):
        sent.append((req.full_url, dict(req.header_items()), timeout))
        return Resp(body, link if len(sent) == 1 else None)
    monkeypatch.setattr(cmf.urllib.request, "urlopen", urlopen)
    data, nxt = cmf._get("https://api.github.com/x", "tok")
    assert data[0]["ref"] == "refs/heads/main"
    assert nxt == "https://api.github.com/repositories/1/activity?after=abc"
    assert cmf._get("https://api.github.com/x", None) == (data, None)
    (url, with_token, timeout), (_, without, _) = sent
    assert with_token["Authorization"] == "Bearer tok" and "Authorization" not in without
    assert with_token["Accept"] == "application/vnd.github+json" and timeout == 30
    monkeypatch.setattr(cmf.urllib.request, "urlopen", lambda req, timeout: Resp(b""))
    with pytest.raises(cmf.CannotJudge):                 # an empty body is no answer
        cmf.mirror_pushes("o/r", None, SINCE)


# --- what a run says and returns ---

def _main(pushes, now, *argv):
    return cmf.main(list(argv), now=now, pushes_of=lambda repo, token, since: pushes)


def test_exit_codes(capsys, monkeypatch):
    now = _m("2026-11-16T13:30:00")
    good = _publishes("2026-11-01", now)
    assert _main(good, now) == 0
    out = capsys.readouterr().out
    assert "FRESH" in out and "0 late window(s) in the last 7 days" in out
    one = [p for p in good if not _m("2026-11-16T12:30:00") < p < _m("2026-11-16T13:00:00")]
    assert _main(one, now) == 0
    out = capsys.readouterr().out
    assert "LATE: no push within an hour of the 12:30 Madrid window of 2026-11-16" in out
    assert "1 late window(s)" in out
    two = [p for p in one if not _m("2026-11-16T11:00:00") < p < _m("2026-11-16T11:30:00")]
    assert _main(two, now) == 1
    out = capsys.readouterr().out
    assert "STALE" in out and "2026-11-15T16:01Z" in out and "server's journal" in out

    def unreadable(repo, token, since):
        raise cmf.CannotJudge("the mirror's push history cannot be read from GitHub")
    assert cmf.main([], now=now, pushes_of=unreadable) == 2
    assert "CANNOT JUDGE" in capsys.readouterr().out

    def no_zone():
        raise cmf.CannotJudge("no zone")
    monkeypatch.setattr(cmf, "_madrid", no_zone)
    assert _main(good, now) == 2


def test_a_missing_zone_database_cannot_be_judged(monkeypatch, capsys):
    """The alert's reminder lists missing time-zone data under CANNOT JUDGE:
    the run must say exactly that, and exit 2."""
    monkeypatch.setitem(sys.modules, "zoneinfo", None)      # the import fails, as with no tz data
    with pytest.raises(cmf.CannotJudge, match="Europe/Madrid"):
        cmf._madrid()
    assert _main([], _z("2026-11-16T12:30:00")) == 2
    out = capsys.readouterr().out
    assert out.startswith("mirror-freshness: CANNOT JUDGE: the Europe/Madrid zone cannot be read")


def test_the_token_the_repo_and_a_weeks_reach_get_to_the_reader(monkeypatch):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    now = _m("2026-11-16T13:30:00")
    cmf.main(["--repo", "o/r"], now=now,
             pushes_of=lambda repo, token, since: seen.append((repo, token, since)) or [])
    assert seen == [("o/r", "tok", now - timedelta(days=7, minutes=5))]
    assert cmf.LATE_DAYS == 7
    assert cmf.MIRROR == "ArturoQuintana/talea"


def test_the_drill_fails_on_purpose_without_reading_anything(capsys):
    def never(repo, token, since):
        raise AssertionError("a drill reads nothing")
    assert cmf.main(["--drill"], now=_m("2026-11-16T13:30:00"), pushes_of=never) == 1
    assert "DRILL" in capsys.readouterr().out


def test_it_runs_stdlib_only_as_the_workflow_runs_it():
    """`-I -S`: no site-packages, no project; Madrid's zone comes from the
    system database. The drill reads nothing, so this needs no network."""
    r = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "scripts" / "check_mirror_freshness.py"),
                        "--drill"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DRILL" in r.stdout


# --- the premise: every publish makes a commit ---

def test_every_render_differs_from_the_minute_before():
    """The rule reads "no push" as "nothing published". That holds because each
    page prints the minute it was generated, so a publish with no new price
    still changes the page, and so the mirror gets a commit."""
    rd_spec = importlib.util.spec_from_file_location("render_dashboard", ROOT / "scripts" / "render_dashboard.py")
    rd = importlib.util.module_from_spec(rd_spec)
    rd_spec.loader.exec_module(rd)
    t = datetime(2026, 11, 16, 10, 1, tzinfo=UTC)
    assert rd.build("es", now=t) != rd.build("es", now=t + timedelta(minutes=1))


# --- the workflow ---

def _workflow() -> str:
    return private_file(".github/workflows/mirror-freshness.yml").read_text()


def test_the_workflow_is_read_only_and_runs_the_check_as_tested():
    text = _workflow()
    assert re.search(r"^permissions:\n  contents: read\n\n", text, re.M)
    assert "write" not in text.split("permissions:", 1)[1].split("jobs:", 1)[0]
    assert 'uv run --no-project python -I -S scripts/check_mirror_freshness.py "$@"' in text
    assert re.search(r"push:\n    branches: \[main\]", text)
    assert "workflow_dispatch:" in text and "drill:" in text
    assert 'if [ "$DRILL" = true ]; then set -- --drill; fi' in text


def test_each_window_is_checked_by_a_scheduled_run_after_its_grace_all_year():
    """When the server pushes nothing, the schedule is the only trigger: each
    window must be the newest due window of some scheduled run, in winter and
    in summer, so that no window goes unjudged."""
    crons = [(int(h), int(m)) for m, h in re.findall(r'- cron:\s*"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*"', _workflow())]
    assert len(crons) == len(cmf.WINDOWS)
    for day in ("2026-12-15", "2027-07-15"):
        wins = [_m(f"{day}T{h:02d}:{m:02d}:00") for h, m in cmf.WINDOWS]
        runs = [_z(f"{day}T{h:02d}:{m:02d}:00") for h, m in crons]
        assert sorted(cmf.due_windows(r)[0] for r in runs) == wins, day
