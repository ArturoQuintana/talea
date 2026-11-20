"""D-36's triggers T1 and T2: a slow write, or a receipt stamped close to its gate (register T-41)

    GITHUB_TOKEN=… uv run --no-project python -I scripts/check_deadline_margins.py --trigger T1|T2 \
        [--repo OWNER/NAME] [--ref HEAD] [--days 7] [--since DATE|TIME] [--until TIME] [--items FILE]

The operator ruled D-36 on 2026-11-20: "no margin, adopt T1–T4". The loop is
unchanged: no receipt is refused in the last seconds before its gate. Instead,
four reconsideration triggers are named to him. When one fires, D-36 is open
again for his decision; the trigger changes no rule by itself. T3 and T4 come
from scripts/check_receipt_push_times.py. This script reports the other two,
ONE PER RUN (`--trigger`): each trigger is its own check, with its own workflow
(write-gap.yml for T1, stamp-margin.yml for T2) and its own red-main issue, so
a chronic trigger never hides another kind (his second ruling, "C1"). A red
run's workflow emails him the lines that set the verdict ("C2",
scripts/trigger_email.py). With `--items` the check writes them to that file,
newest first. Its path is checked before the check runs (an existing path is
refused, never overwritten), and the file is created, exclusively, only after
the check returned, so a crash leaves none. If it cannot be created or written
then, the verdict and its result line stand, one line says "could not write the
items list", and nothing is deleted; a write that fails midway can leave a
partial list, so the email may list fewer items while the verdict, its label
and the run log stay complete. It is the only thing the check writes, outside
the checkout. It
is observe-only: it reads this repository and GitHub and writes nothing else,
not even Python bytecode.

  T1  A WRITE GAP OF 100 MS OR MORE: the time from a receipt's clock reading
      (its stamp) to the write of its line, as the writer logged it in the
      BODY of the commit that carried the receipt (register T-35):
        "Write gap, last receipt per file (<assumptions>): es ≤10 ms, de ≤10 ms"
      The server's Data commit has carried that line since 2026-11-16. The
      runners' commits (ercot.yml, jp.yml) carry it in the same format, built
      from the tick's own line. The logged figure is an UPPER bound (the gap
      plus the file clock's 10 ms resolution, rounded up), and it is compared
      to 100 ms as logged, with nothing subtracted. T1 can therefore fire for a
      true gap as small as about 90 ms. That errs toward reporting, which is
      right for a reconsideration trigger.
  T2  A RECEIPT STAMPED WITHIN 60 S OF ITS MARKET'S DEADLINE (60 s or less
      before it, inclusive): the
      receipt's own `committed_at` against its deadline (`basis_day` at the
      market's `deadline_hour`, in the market's zone: the push-time check's own
      `deadline()`). A stamp at or after the deadline, which the clock guard
      refuses, is T2 as well (it errs toward reporting), and the line says
      "after it".

T1 CANNOT JUDGE (not a trigger): a commit that added receipts to a market's
file, which GitHub received on or after the start date, and whose body has no
write-gap entry for that market, or says "not measured". This is DETECTOR
HEALTH: without the line, T1 is blind. It is reported (exit 2), and it never
reopens D-36. The other CANNOT JUDGE cases (a push history that cannot be read
or does not reach back, a clone that cannot be read, a commit that cannot be
told, a receipt line without a readable stamp or day) are detector health too.

WHICH RECEIPTS: the push-time check's own walk (`collect`): the receipts on
first-parent commits of `--ref` that GitHub received in the last `--days`
days, selected by GitHub's push record, never by a stamp or a commit date.
`--since` and `--until` mean what they mean there (`--since` also takes an ISO
time with a zone: the workflow passes 30 minutes after the merge that added it,
found on main's line). A receipt or commit GitHub
received before `--since` is judged and listed as information. It does not set
the exit code (the history before the ruling, e.g. 12 JP receipts on 3 basis
days, 2026-10-07, 10-31 and 11-02, would be T2). One with no push in the record
always sets the exit code.

STATED LIMITS: T1 sees only the LAST receipt per file in each commit (the
logged gap is the file's time minus the last line's stamp). The other writes
of a pass are not measured: measuring each would touch loop.py, which an
observe-only check does not. T2 judges the writer's own clock. A clock that
runs SLOW stamps earlier than the true time, so it hides a write close to the
gate (T3, GitHub's push time, covers that side); one that runs fast stamps
later, so it can fire a T2 for a write that was not that close. A commit that merges a branch carrying receipts has no gap line, so
it is T1 CANNOT JUDGE.

The last line is "<tag> result: <word>" whenever the check finished, the tag
being "[write-gap]" (T1) or "[stamp-margin]" (T2): NONE FIRED (exit 0); T1 or
T2 (exit 1); for T1 "T1 CANNOT JUDGE (not a trigger)" when the only problems are
missing or unmeasured gaps; otherwise "CANNOT JUDGE (not a trigger)" (exit 2).
Exit 1 wins over 2, and the check then prints "<tag> also CANNOT JUDGE: <N>"
first. Anything unexpected is
exit 2 ("CANNOT JUDGE: unexpected error (<class>)"). Only dates, UTC times,
whole milliseconds, seconds, short commit ids and (strategy, version) pairs
registered in the code are printed. Nothing from a commit's body is echoed, and
no price.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True                  # read-only: the import below writes no __pycache__
sys.path[:0] = [s for s in (str(Path(__file__).resolve().parent),) if s not in sys.path]
import check_receipt_push_times as cpt  # noqa: E402  (the shared walk: one implementation)

ROOT = cpt.ROOT
T1_MS = 100                                     # T1: a logged write gap of 100 ms or more
T2_S = 60                                       # T2: a stamp 60 s or less before the deadline
TAGS = {"T1": "[write-gap]", "T2": "[stamp-margin]"}
GAP_LINE = re.compile(r"^Write gap, last receipt per file \(.*?\): (.*)$", re.M)
GAP_ENTRY = re.compile(r"([a-z][a-z0-9_-]*) (?:≤([0-9]{1,9}) ms|(not measured)\b.*)")
T1_HEALTH = "T1 CANNOT JUDGE (not a trigger)"
HEALTH = "CANNOT JUDGE (not a trigger)"


def gap_ms(body: str, slug: str) -> int | str:
    """The logged write gap for `slug` in a commit's body: whole milliseconds,
    or "not measured" when the writer said so, or "no line" when the body has
    no entry for that market. Only the first write-gap line is read."""
    m = GAP_LINE.search(body)
    if not m:
        return "no line"
    for part in m.group(1).split(", "):
        e = GAP_ENTRY.fullmatch(part.strip())
        if e and e.group(1) == slug:
            return int(e.group(2)) if e.group(2) else "not measured"
    return "no line"


def _body(repo: Path, commit: str) -> str:
    """The commit's BODY only (%b): a write-gap line in a subject is not the
    writers' line, which they put in the body."""
    return cpt._git(repo, "log", "-1", "--format=%b", commit)


def margin_s(receipt: dict, market) -> float:
    """Seconds from the receipt's stamp to its deadline (negative: after it)."""
    return (cpt.deadline(receipt, market) - cpt._at(receipt["committed_at"])).total_seconds()


def _newest(added: list, commit: str) -> str:
    """The newest target date among the receipts `commit` added (as printed)."""
    days = [cpt.shown(a.receipt, "target") for a in added if a.commit == commit]
    days = [d for d in days if d != "<invalid>"]
    return max(days) if days else ""


def run(repo_dir: Path, repo: str, ref: str, now: datetime, days: int, markets: dict,
        token: str | None, trigger: str, get=cpt._get, start: datetime | None = None,
        pairs: set[tuple[str, str]] | None = None,
        until: datetime | None = None) -> tuple[int, str, list[tuple[str, str]]]:
    """(exit code, result word, the items that set the verdict as (date, line)).
    `trigger` is "T1" or "T2": one check per trigger, each its own workflow and
    red-main issue (ruled 2026-11-20, C1). Nothing is selected or excused by a
    stamp."""
    if trigger not in TAGS:
        raise ValueError(f"trigger must be one of {sorted(TAGS)}")
    tag = TAGS[trigger]
    pairs = cpt._strategies() if pairs is None else pairs
    try:
        w = cpt.collect(repo_dir, repo, ref, now, days, markets, token, get=get, until=until)
        bodies = ({c: _body(repo_dir, c) for added, _ in w.found.values() for c in {a.commit for a in added}}
                  if trigger == "T1" else {})
    except cpt.CannotRead as exc:
        print(f"{tag} CANNOT JUDGE: {exc}")
        return 2, HEALTH, [("", f"{HEALTH}: {exc}")]
    print(f"{tag} receipts whose commit reached main since {w.since:%Y-%m-%dT%H:%MZ} "
          f"(after {w.base[:7] if w.base else 'the start of the line'}); GitHub's push history "
          f"{'reaches back past it' if w.reaches else 'ends before it'}")
    if start is not None:
        print(f"{tag} the verdict covers receipts and commits GitHub received from "
              f"{start:%Y-%m-%dT%H:%MZ} (and any with no push in the record); earlier ones are information")
    fired = 0
    health = {T1_HEALTH: 0, HEALTH: 0}
    notes: list[str] = []
    items: list[tuple[str, str]] = []

    def note(day: str, line: str, earlier: bool) -> None:
        notes.append(line + (" (before the start date: information)" if earlier else ""))
        if not earlier:
            items.append((day, line))

    if not w.reaches:
        health[HEALTH] += 1
        note("", f"{HEALTH}: GitHub's push history ends before the window's start, so the window "
                 "cannot be shown complete", False)
    for slug, (added, untold) in w.found.items():
        market = markets[slug]
        what = "receipts" if trigger == "T2" else "commits"
        n = {what: 0, trigger: 0, **({T1_HEALTH: 0} if trigger == "T1" else {}), HEALTH: 0}
        info = {k: 0 for k in n if k != what}
        for c in untold:
            n[HEALTH] += 1
            note("", f"{slug}: {HEALTH}: commit {c[:7]}: the file there does not begin with the file "
                     "at its first parent, or a line is not a JSON object", False)
        if trigger == "T2":
            for a in added:
                at = w.pushed.get(a.commit)
                earlier = start is not None and at is not None and at < start
                count = info if earlier else n
                if not earlier:
                    n["receipts"] += 1
                r = a.receipt
                day = cpt.shown(r, "target")
                try:
                    s = margin_s(r, market)
                except (KeyError, TypeError, ValueError, AttributeError):
                    count[HEALTH] += 1
                    note("" if day == "<invalid>" else day,
                         f"{slug}: {HEALTH}: a receipt of commit {a.commit[:7]} has no readable stamp or day",
                         earlier)
                    continue
                if s <= T2_S:
                    count["T2"] += 1
                    strategy, version = cpt.shown_strategy(r, pairs)
                    where = f"{s:.1f} s before it" if s > 0 else f"{-s:.1f} s after it"
                    note(day, f"{slug}: TRIGGER T2 {day} [{strategy} v{version}] stamped "
                              f"{cpt.shown(r, 'committed_at')}, deadline {cpt.deadline(r, market).isoformat()}, "
                              f"{where} (commit {a.commit[:7]})", earlier)
        else:
            for c in dict.fromkeys(a.commit for a in added):           # each commit once, oldest first
                at = w.pushed.get(c)
                earlier = start is not None and at is not None and at < start
                count = info if earlier else n
                if not earlier:
                    n["commits"] += 1
                g = gap_ms(bodies[c], slug)
                if isinstance(g, int):
                    if g >= T1_MS:
                        count["T1"] += 1
                        note(_newest(added, c), f"{slug}: TRIGGER T1 commit {c[:7]}: write gap ≤{g} ms logged "
                                                f"(an upper bound; T1 is {T1_MS} ms or more)", earlier)
                else:
                    count[T1_HEALTH] += 1
                    said = "no write-gap entry for this market" if g == "no line" else 'the gap "not measured"'
                    note(_newest(added, c), f"{slug}: {T1_HEALTH}: commit {c[:7]} carries {said}", earlier)
        print(f"{tag} {slug}: " + ", ".join(f"{k} {v}" for k, v in n.items())
              + ("; before the start date: " + ", ".join(f"{k} {v}" for k, v in info.items())
                 if start is not None else ""))
        fired += n[trigger]
        for k in health:
            health[k] += n.get(k, 0)
    for line in notes:
        print(f"{tag} {line}")
    if fired:
        if any(health.values()):
            print(f"{tag} also CANNOT JUDGE: {sum(health.values())}")
        return 1, trigger, items
    if health[HEALTH]:
        return 2, HEALTH, items
    if health[T1_HEALTH]:
        return 2, T1_HEALTH, items
    return 0, "NONE FIRED", items


def main(argv: list[str] | None = None, now: datetime | None = None) -> tuple[int, str, str]:
    """(exit code, result word, the trigger's log tag)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--trigger", required=True, choices=sorted(TAGS),
                    help="T1 (a write gap of 100 ms or more) or T2 (a stamp within 60 s of the deadline)")
    ap.add_argument("--items", default=None,
                    help="write the items that set the verdict to this file, newest first (for the email)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="OWNER/NAME on GitHub (default: $GITHUB_REPOSITORY)")
    ap.add_argument("--ref", default="HEAD", help="the main line to walk (default: HEAD)")
    ap.add_argument("--days", type=int, default=7,
                    help="receipts on commits GitHub received in the last N days")
    ap.add_argument("--since", default=None,
                    help="start (YYYY-MM-DD, UTC midnight, or an ISO time with a zone): only what GitHub "
                         "received from it sets the exit code")
    ap.add_argument("--until", default=None,
                    help="the time the clone was fetched (ISO, with a zone): later pushes are left for the next run")
    args = ap.parse_args(argv)
    if not args.repo:
        ap.error("--repo OWNER/NAME is needed (or GITHUB_REPOSITORY)")
    try:
        start = None
        if args.since:
            start = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                     if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since) else cpt._at(args.since))
    except ValueError:
        ap.error("--since must be a date, YYYY-MM-DD, or an ISO time with a zone")
    try:
        until = cpt._at(args.until) if args.until else None
    except ValueError:
        ap.error("--until must be an ISO time with a zone, e.g. 2026-11-17T18:23:05Z")
    out = cpt.items_file(ap, args.items)          # checked now; created only after the check returned
    rc, word, items = run(ROOT, args.repo, args.ref, now or datetime.now(timezone.utc), args.days,
                          cpt._registry(), os.environ.get("GITHUB_TOKEN"), args.trigger, start=start,
                          until=until)
    cpt.write_items_to(out, items, TAGS[args.trigger])
    return rc, word, TAGS[args.trigger]


def entry(argv: list[str] | None = None) -> int:
    """main(), and the one line a caller can trust, printed last whenever the
    check finished. Anything unexpected is exit 2 with fixed words (Python's
    own exit for an uncaught exception is 1, which must mean a trigger)."""
    argv = sys.argv[1:] if argv is None else argv
    tag = next((TAGS[v] for k, v in zip(argv, argv[1:]) if k == "--trigger" and v in TAGS), "[deadline-margins]")
    try:
        rc, word, tag = main(argv)
    except SystemExit:
        raise                               # argparse's own refusals (exit 2), unchanged
    except Exception as exc:  # noqa: BLE001
        print(f"{tag} CANNOT JUDGE: unexpected error ({type(exc).__name__})")
        rc, word = 2, HEALTH
    print(f"{tag} result: {word}")
    return rc


if __name__ == "__main__":
    sys.exit(entry())
