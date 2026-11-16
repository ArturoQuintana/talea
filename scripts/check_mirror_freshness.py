"""Has the public mirror stopped updating? (register T-28)

    uv run --no-project python -I -S scripts/check_mirror_freshness.py [--repo OWNER/NAME] [--drill]

The server publishes the mirror right after each of its three daily windows,
11:00, 12:30 and 17:00 on Madrid's clock (docs/calendar.md, section 2, "the
server's four windows"; the fourth, Sunday 12:00, is the weekly maintenance run,
not a publish). Every publish makes a commit even when no price moved: each
rendered page prints the minute it was generated ("data as of …",
scripts/render_dashboard.py, `asof` in build()), and the builder renders on
every run (scripts/mirror_tree.sh). So a window with no commit means the
publisher published nothing.

THE RULE: a window W is MISSED when the mirror shows no push to main between
W - 5 minutes and W + 60 minutes. The mirror is STALE when the two newest due
windows (those whose W + 60 minutes has passed) are both missed and no push has
come since: it has fallen behind by more than one tick (register T-28's
wording). One missed window alone is LATE: exit 0, and the run prints the
missed windows of the last seven days (a push that failed and rode the next
publish, as on 2026-11-04 and 11-05).
Windows are computed on Madrid's clock (zoneinfo), so the rule never has to be
rewritten when the clocks change. 60 minutes covers the ticks before the
publish, Spain's fetch retries (5 + 15 minutes) among them; measured on
2026-11-01..15, every one of 42 publishes came 0.6 to 1.6 minutes after its
window.

THE TIME COMPARED is GitHub's own record of the push (the repository activity
API), not the commit's date: the server's clock sets the commit date, GitHub sets
the push time. Its pages are followed until the last seven days are covered;
when that cannot be shown, the check cannot judge.

Exit codes: 0 fresh; 1 stale (or a drill); 2 the check cannot judge (the API
cannot be read, an answer is not what it should be, the zone database is
missing). Both 1 and 2 fail the workflow run, and the red-main alert reports it.

--drill judges an empty push list against the newest due window: it always
exits 1, saying DRILL, to test the reporting path. It needs no server and reads
no data.

What it cannot see is stated in INFRA.md ("The mirror-freshness check").
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

MIRROR = "ArturoQuintana/talea"
WINDOWS = ((11, 0), (12, 30), (17, 0))      # Madrid clock, every day (docs/calendar.md)
GRACE = timedelta(minutes=60)
EARLY = timedelta(minutes=5)
LOOKBACK_DAYS = 3
LATE_DAYS = 7                                # the LATE list's reach
PER_PAGE = 100
MAX_PAGES = 30                               # 3,000 pushes: far beyond a week's 21
API = "https://api.github.com/"


class CannotJudge(Exception):
    """Exit 2: the check cannot judge, which is never a pass."""


def _madrid():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Madrid")
    except Exception:
        raise CannotJudge("the Europe/Madrid zone cannot be read (is the time-zone database installed?)") from None


def due_windows(now: datetime) -> list[datetime]:
    """Every publish window W of the last LOOKBACK_DAYS with W + GRACE <= now,
    newest first, as UTC instants."""
    zone = _madrid()
    local = now.astimezone(zone)
    out = []
    for back in range(LOOKBACK_DAYS + 1):
        day = (local - timedelta(days=back)).date()
        for hh, mm in WINDOWS:
            w = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone).astimezone(timezone.utc)
            if w + GRACE <= now:
                out.append(w)
    return sorted(out, reverse=True)


def missed(w: datetime, pushes: list[datetime]) -> bool:
    return not any(w - EARLY <= p <= w + GRACE for p in pushes)


def judge(pushes: list[datetime], now: datetime) -> tuple[str, datetime, datetime | None]:
    """("fresh" | "late" | "stale", the newest due window, the newest push).
    STALE: the two newest due windows are both missed and nothing has been
    pushed since the earlier of them. LATE: the newest due window is missed
    but the mirror is not stale."""
    w1, w2 = due_windows(now)[:2]
    newest = max(pushes) if pushes else None
    if missed(w1, pushes) and missed(w2, pushes) and (newest is None or newest < w2 - EARLY):
        return "stale", w1, newest
    return ("late" if missed(w1, pushes) else "fresh"), w1, newest


def late_windows(pushes: list[datetime], now: datetime, days: int = LATE_DAYS) -> list[datetime]:
    """The missed windows of the last `days` days, oldest first."""
    zone = _madrid()
    local = now.astimezone(zone)
    out = []
    for back in range(days + 1):
        day = (local - timedelta(days=back)).date()
        for hh, mm in WINDOWS:
            w = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone).astimezone(timezone.utc)
            if w + GRACE <= now and now - w <= timedelta(days=days) and missed(w, pushes):
                out.append(w)
    return sorted(out)


def _get(url: str, token: str | None):
    """(the parsed answer, the next page's URL or None), from the Link header."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        link = resp.headers.get("Link") or ""
        body = json.loads(resp.read() or b"null")
    nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return body, (nxt.group(1) if nxt else None)


def mirror_pushes(repo: str, token: str | None, since: datetime, get=_get) -> list[datetime]:
    """GitHub's push times to the mirror's main, newest first, every one since
    `since`. GitHub answers newest first, a page at a time; pages are followed
    until one reaches back past `since` or there is no next page (the history is
    exhausted, shown by a short page with no next one). If that cannot be
    established (more than MAX_PAGES pages, a full page with no next one, a next
    page that is not on GitHub's API, entries out of order), the check cannot
    judge. The workflow's token is scoped to its own repository; if GitHub
    refuses it for the mirror, the read is repeated without one: the mirror is
    public."""
    url = (f"{API}repos/{repo}/activity"
           f"?ref=refs/heads/main&activity_type=push&per_page={PER_PAGE}")
    out: list[datetime] = []
    stamps: list[datetime] = []
    for page in range(MAX_PAGES):
        try:
            try:
                data, nxt = get(url, token)
            except urllib.error.HTTPError as exc:
                if not token or exc.code not in (401, 403, 404) or page:
                    raise
                token = None
                data, nxt = get(url, None)
        except (urllib.error.URLError, OSError, ValueError):
            raise CannotJudge("the mirror's push history cannot be read from GitHub") from None
        if not isinstance(data, list):
            raise CannotJudge("GitHub's answer about the mirror's pushes is not a list")
        for item in data:
            try:
                ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    raise CannotJudge("a push time has no time zone")
                stamps.append(ts)
                if item["activity_type"] == "push" and item["ref"] == "refs/heads/main":
                    out.append(ts)
            except (KeyError, TypeError, AttributeError, ValueError):
                raise CannotJudge("an entry of GitHub's push history cannot be read") from None
        if any(a < b for a, b in zip(stamps, stamps[1:])):
            raise CannotJudge("GitHub's push history is not newest first, so its completeness cannot be shown")
        if stamps and stamps[-1] < since:
            return out
        if nxt is None:
            if len(data) >= PER_PAGE:        # a full page that names no next one: not proof of the end
                raise CannotJudge("a full page of pushes with no next page: the history's end cannot be shown")
            return out
        if not nxt.startswith(API):
            raise CannotJudge("GitHub's next page is not on its API")
        url = nxt
    raise CannotJudge(f"more than {MAX_PAGES} pages of pushes: the last {LATE_DAYS} days cannot be shown complete")


def _madrid_label(w: datetime) -> str:
    return w.astimezone(_madrid()).strftime("the %H:%M Madrid window of %Y-%m-%d")


def main(argv: list[str] | None = None, now: datetime | None = None, pushes_of=mirror_pushes) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=MIRROR, help="the public mirror (default: %(default)s)")
    ap.add_argument("--drill", action="store_true", help="judge an empty push list: always exit 1, saying DRILL")
    args = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    try:
        since = now - timedelta(days=LATE_DAYS) - EARLY
        pushes = [] if args.drill else pushes_of(args.repo, os.environ.get("GITHUB_TOKEN"), since)
        state, w, newest = judge(pushes, now)
        late = late_windows(pushes, now)
    except CannotJudge as e:
        print(f"mirror-freshness: CANNOT JUDGE: {e}")
        return 2
    last = f"the last push was {newest:%Y-%m-%dT%H:%MZ}" if newest else "no push is on record"
    if args.drill:
        print(f"mirror-freshness: DRILL: judged with no push against {_madrid_label(w)}; "
              f"this run fails on purpose, to test the alert")
        return 1
    for lw in late:
        print(f"mirror-freshness: LATE: no push within an hour of {_madrid_label(lw)}")
    print(f"mirror-freshness: {len(late)} late window(s) in the last 7 days")
    if state == "stale":
        age = f" ({(now - newest).total_seconds() / 3600:.1f} h ago)" if newest else ""
        print(f"mirror-freshness: STALE: the public mirror has missed two windows in a row, up to "
              f"{_madrid_label(w)}; {last}{age}. The publisher's reason, if any, is in the server's journal.")
        return 1
    print(f"mirror-freshness: {state.upper()}: {last}; newest window judged: {_madrid_label(w)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
