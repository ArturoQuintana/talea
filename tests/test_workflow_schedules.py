"""GitHub Actions schedule reliability (closed defect loop, 2026-09-01
infra-continuity audit finding #2): a single scheduled cron is a single point
of failure — GitHub Actions cron has been observed firing 6-14h late even
against a supposed multi-hour safety margin, degrading ERCOT's receipt
coverage (2 of its last 4 scheduled runs missed their commit window; the
clock/leak guard correctly refused rather than leak, but coverage suffered).
The fix is a RETRY LADDER: multiple independent schedule entries, each still
safely before the market's hard commit deadline, so one bad delay doesn't
sink the whole day (the tick is idempotent, so extra on-time attempts are
free no-ops).

Deliberately regex-based (stdlib-only project, no PyYAML dependency) — reads
the `- cron: "..."` lines under `on: schedule:`, same pragmatic style as
test_docs_in_sync.py's markdown checks."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from talea.markets import MARKETS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# A reference date to resolve each market's local-time deadline to a UTC
# instant (handles DST via the real zone, not a fixed offset).
REFERENCE_DATE = (2026, 9, 1)


def _crons(workflow_name: str) -> list[tuple[int, int]]:
    """[(hour, minute), ...] UTC for every `- cron: "M H * * *"` line in the
    named workflow file's `schedule:` block."""
    text = (WORKFLOWS / workflow_name).read_text()
    return [(int(h), int(m)) for m, h in
            re.findall(r'- cron:\s*"(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*"', text)]


def _deadline_utc_hour(slug: str) -> float:
    """The market's hard commit-deadline (deadline_hour, local tz) expressed
    as a UTC hour-of-day float on REFERENCE_DATE."""
    m = MARKETS[slug]
    y, mo, d = REFERENCE_DATE
    local = datetime(y, mo, d, m.deadline_hour, 0, tzinfo=m.tz)
    utc = local.astimezone(ZoneInfo("UTC"))
    return utc.hour + utc.minute / 60


def test_ercot_schedule_has_a_retry_ladder():
    """A single scheduled cron is a single point of failure against GitHub's
    documented multi-hour scheduling delays. Regression guard for the
    2026-09-01 finding: don't collapse back to one shot."""
    crons = _crons("ercot.yml")
    assert len(crons) >= 2, (
        "ercot.yml should schedule multiple independent attempts (a retry "
        "ladder), not rely on a single cron entry that a late GitHub Actions "
        "fire can sink for the whole day")


def test_ercot_schedule_entries_stay_before_the_commit_deadline():
    """Every rung of the ladder must itself fire with real margin before the
    10:00 America/Chicago hard cutoff — a ladder that schedules an entry past
    (or too close to) the deadline defeats its own purpose."""
    deadline = _deadline_utc_hour("ercot")
    min_margin_hours = 1.0
    for hour, minute in _crons("ercot.yml"):
        scheduled = hour + minute / 60
        margin = deadline - scheduled
        assert margin >= min_margin_hours, (
            f"ercot.yml cron {hour:02d}:{minute:02d} UTC leaves only "
            f"{margin:.2f}h before the {deadline:.2f} UTC commit deadline")


# --- JP: the same ladder, but its window wraps the UTC day -----------------------

def _local_hour(slug: str, hour: int, minute: int) -> float:
    """A UTC cron time as the market's local hour-of-day float on REFERENCE_DATE."""
    y, mo, d = REFERENCE_DATE
    local = datetime(y, mo, d, hour, minute, tzinfo=ZoneInfo("UTC")).astimezone(MARKETS[slug].tz)
    return local.hour + local.minute / 60


def test_jp_schedule_has_a_retry_ladder():
    """Incident 2026-11-10: JP ran on ONE cron entry (21:37 UTC, 3h23 before the
    10:00 JST gate). GitHub started it 1.7-6.1h late, so it committed a receipt on
    25 of its last 40 runs and on 1 of the last 9 — a market going dark by honest
    misses. Don't collapse back to one shot."""
    assert len(_crons("jp.yml")) >= 3, (
        "jp.yml should schedule several independent attempts (a retry ladder), "
        "not a single cron entry that one late GitHub Actions start sinks")


def test_jp_schedule_entries_fire_inside_the_commit_window():
    """Every rung must land between local midnight and the gate. Before JST
    midnight 'tomorrow' is a day JEPX has already published (the leak guard
    refuses — a wasted run); too close to 10:00 JST and the usual multi-hour
    start delay turns it into a miss. And the FIRST rung must sit early enough
    to absorb the delays actually observed (6.1h on JP, longer on ERCOT)."""
    deadline = MARKETS["jp"].deadline_hour
    margins = []
    for hour, minute in _crons("jp.yml"):
        local = _local_hour("jp", hour, minute)
        margin = deadline - local
        assert 0 <= local < deadline, (
            f"jp.yml cron {hour:02d}:{minute:02d} UTC is {local:.2f}h JST — outside "
            f"the 00:00-{deadline}:00 JST commit window")
        assert margin >= 3.0, (
            f"jp.yml cron {hour:02d}:{minute:02d} UTC leaves only {margin:.2f}h "
            f"before the {deadline}:00 JST gate")
        margins.append(margin)
    assert max(margins) >= 8.0, (
        f"the earliest JP rung leaves {max(margins):.2f}h — it must start right "
        f"after JST midnight to survive the start delays this repo actually sees")


def test_every_jp_rung_commits_when_it_starts_on_time(tmp_path):
    """The ladder's premise, run through the real tick: at each rung's JST clock
    time, with today's profile complete (JEPX published it yesterday) and
    tomorrow's not yet out, the tick commits tomorrow's receipt — including the
    00:13 JST rung, minutes after the market-local day turned over."""
    from datetime import date

    import talea.loop as loop
    jp = MARKETS["jp"]
    today = date(*REFERENCE_DATE)
    prices = {f"{today.isoformat()}T{h:02d}": 10000.0 + 100 * h for h in range(24)}
    for i, (hour, minute) in enumerate(_crons("jp.yml")):
        local = _local_hour("jp", hour, minute)
        now = datetime(today.year, today.month, today.day, int(local),
                       round((local % 1) * 60), tzinfo=jp.tz)
        mkt = loop.Market.make("jp", jp.tz, fetch=None, deadline_hour=jp.deadline_hour,
                               currency=jp.currency, root=tmp_path / f"rung{i}")
        s = loop.tick(market=mkt, fetch=lambda a, b: prices, now_fn=lambda: now,
                      sleep=lambda _s: None)
        assert s["committed"], f"the {hour:02d}:{minute:02d} UTC rung ({now:%H:%M} JST) did not commit: {s['skipped']}"
        assert {r["target"] for r in s["committed"]} == {"2026-09-02"}


# --- the origin-side liveness detector ------------------------------------------

def test_origin_liveness_detector_is_scheduled():
    """Incident 2026-11-03..09: the server's liveness check reads the SERVER's
    clone, so a diverged clone (pushes rejected, pulls refused) was invisible to
    it for six days. A scheduled job must run the same monitor against ORIGIN's
    main, fail when it reports STALE, and stay read-only. It must not swallow
    the exit code (`|| true`, a pipe) and must not use the retired phone push."""
    text = (WORKFLOWS / "liveness.yml").read_text()
    assert _crons("liveness.yml"), "liveness.yml must run on a schedule"
    runs = [ln.split("run:", 1)[1].strip() for ln in text.splitlines()
            if ln.strip().startswith(("run:", "- run:"))]
    check = [r for r in runs if "scripts/check_liveness.py" in r]
    assert len(check) == 1, "exactly one step runs the monitor"
    assert "||" not in check[0] and "|" not in check[0] and "--quiet" not in check[0], (
        "the monitor's own exit code must decide the job, and its per-market "
        "lines must reach the run log the alert links to")
    assert "contents: read" in text and "git push" not in text and "git commit" not in text
    assert "ntfy" not in text.split("jobs:", 1)[1]
