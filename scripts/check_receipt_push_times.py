"""Did each receipt reach GitHub before its market's deadline? (register T-35, D-36)

    GITHUB_TOKEN=… uv run --no-project python -I scripts/check_receipt_push_times.py \
        [--repo OWNER/NAME] [--ref HEAD] [--days 7] [--since YYYY-MM-DD] [--until TIME]

A receipt's own stamp (`committed_at`) is a reading of the machine's clock, and
the clock guard in loop.py refuses a receipt whose stamp is at or past its
market's deadline. What no check looked at is when the receipt left that
machine: GitHub's record of the push that delivered it is a time the server
cannot set. This script reads it, read-only, for every receipt whose commit
reached main in the last `--days` days, and gives each one of three verdicts:

  ON TIME       GitHub received it before its market's deadline (the receipt's
                `basis_day` at the market's `deadline_hour`, in the market's
                zone, from the registry);
  UNVERIFIABLE  GitHub received it at or after the deadline. Its stamp may be
                honest (a push that failed and went out with a later one), but
                GitHub cannot vouch that it existed before the gate;
  CANNOT JUDGE  no push in GitHub's record delivered the commit that added it,
                or that commit could not be told (the file's history is not
                append-only there, a line cannot be read).

It never says "leak": that is the leak guard's word (a receipt for day T
committed while a price for T existed), judged in loop.py and by the auditors,
not here. Only the first-parent history of `--ref` (main) is walked, so a
commit on a pull request's branch never counts.

NEVER BY THE STAMP. The window and the start date are judged on what GitHub
and git vouch for, never on a receipt's own `committed_at` nor on a commit's
date (both are the writer's clock, the very thing this check does not trust:
a writer whose clock runs days behind must not push a receipt late and have it
skipped or counted as "information").

HOW, per market (Data/<slug>/receipts.jsonl):
  1. GitHub's repository activity for refs/heads/main (pushes, merges: every
     event that moved main), newest first, page by page until it reaches back
     past the window or the history ends; each event delivered the commits in
     `before..after`. A commit's push time is that of the earliest event that
     delivered it;
  2. THE WALK: the first-parent commits of `--ref` that came after main's tip
     at the window's start (the `before` of the earliest event inside the
     window; with no event inside it, the `after` of the newest one before it;
     with no history at all, the whole line) and changed the file, oldest
     first. The file at each one must begin with the file at its first parent
     (append-only), and the lines after that prefix are the ones it added;
  3. every receipt the walk finds is judged. One with no push in the record is
     CANNOT JUDGE, and counts toward the exit code.

THE LONG-RANGE EVIDENCE (not used here): GitHub keeps the repository's activity
for a limited time, so beyond it a receipt is CANNOT JUDGE. The receipts'
OpenTimestamps proofs (Data/<slug>/ots) carry a calendar's time for the same
bytes and are the evidence for older receipts; reading them is a later option.

THE START DATE (--since): only receipts GitHub received on or after it set the
exit code; a receipt with no push in the record always does. Receipts in the
window received before it are still judged and listed, with the same three
words, as information: a known past case (the divergence incident, row dated
2026-11-09 in docs/incidents.md: basis days 2026-11-04..10, delivered at once
on 2026-11-10T23:03:42Z) does not keep the check red for days, and is not
hidden either.

THE CLONE'S TIME (--until, an ISO time): GitHub's record is read after the
clone was fetched, so it may already list pushes the clone does not hold, and
the clone may hold commits pushed after the time a workflow noted. With
--until, main is judged as GitHub recorded it at that time: pushes after it are
left for the next run, and the walk ends at the commit the newest push up to it
delivered. A workflow notes the time BEFORE it fetches, so every push up to it
is in the clone. Without --until, a push the clone does not hold is a commit
the line does not contain (exit 2). Fail closed on both sides: a commit after
that tip is left for the next run only if a push after --until delivered it
(a push after --until whose range the clone cannot read verifies nothing:
CANNOT JUDGE), and every push up to --until must be contained in that
tip as well (a reset before --until, restored after it, is CANNOT JUDGE).

The last line is "[push-times] result: ON TIME | UNVERIFIABLE | CANNOT JUDGE"
whenever the check finished; anything unexpected is exit 2 ("CANNOT JUDGE:
unexpected error (<class>)"), so exit 1 means UNVERIFIABLE and nothing else.

Exit codes: 0 every receipt that sets the verdict is ON TIME (or there is none);
1 at least one UNVERIFIABLE; 2 at least one CANNOT JUDGE, or the history cannot
be read or does not reach back to the window's start, or the clone cannot be
read: a partial clone (extensions.partialClone set to anything), any remote
with a `promisor` key whatever its value (false included: no value is parsed),
a shallow clone, a push inside the window whose commit
`--ref` does not contain (main reset, or a stale local ref), or a missing
object. 1 wins over 2 when both apply. Only a date, a UTC time and a
(strategy, version) pair registered in the code (of the checkout running this
script) are printed; anything else
prints "<invalid>", and no message echoes git's own output. The script writes
nothing, not even Python bytecode. A summary per market is printed first, then
every receipt that is not ON TIME. Counts and times only: no price.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com/"
PER_PAGE = 100
MAX_PAGES = 30
ZERO = "0" * 40
ON_TIME, UNVERIFIABLE, CANNOT_JUDGE = "ON TIME", "UNVERIFIABLE", "CANNOT JUDGE"


class CannotRead(Exception):
    """Exit 2: the history cannot be read, which is never a pass."""


@dataclass(frozen=True)
class Push:
    at: datetime
    before: str
    after: str


@dataclass(frozen=True)
class Added:
    """A receipt line and the first-parent commit that added it."""
    slug: str
    commit: str
    receipt: dict


def _at(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("a time without a zone")
    return t


# --- git, read-only --------------------------------------------------------
# Every git call goes through _gitrun, never with a prompt. A partial clone
# (one that may fetch missing objects from a promisor remote) is refused
# outright before any object is read, whatever the git version;
# GIT_NO_LAZY_FETCH (honoured by git 2.44 or later) is a second guard.

_GIT_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}


def _gitrun(repo: Path, *args: str, text: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text,
                          env={**os.environ, **_GIT_ENV})


def _git(repo: Path, *args: str) -> str:
    r = _gitrun(repo, *args)
    if r.returncode != 0:
        raise CannotRead(f"git {args[0]} failed")       # fixed words: git's own output never printed
    return r.stdout


def check_clone(repo: Path, ref: str, pushes: list["Push"], since: datetime) -> None:
    """The clone can be judged from: not shallow, and every push inside the
    window delivered a commit that `ref` contains (a main that was reset, or
    a local ref that is stale, is not judged)."""
    # No value is parsed (a remote's name may hold any character, \r and
    # Unicode spaces included): exit 0 = the key exists, exit 1 = it does
    # not, anything else = no judgement. extensions.partialClone with any
    # value is a partial clone; ANY remote.<name>.promisor key, whatever its
    # value (false included), is refused too.
    partial = _gitrun(repo, "config", "-z", "--get", "extensions.partialClone")
    promisors = _gitrun(repo, "config", "--get-regexp", r"^remote\..*\.promisor$")
    if partial.returncode not in (0, 1) or promisors.returncode not in (0, 1):
        raise CannotRead("git config cannot be read, so a partial clone cannot be ruled out")
    if partial.returncode == 0:
        raise CannotRead("partial clone: cannot judge offline")
    if promisors.returncode == 0:
        raise CannotRead("promisor remote configured: cannot judge offline")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise CannotRead("this clone is shallow")
    all_on(repo, ref, pushes, since)


def all_on(repo: Path, tip: str, pushes: list["Push"], since: datetime) -> None:
    """Every push inside the window delivered a commit `tip` contains."""
    for p in pushes:
        if p.at >= since and p.after and p.after != ZERO:
            if _gitrun(repo, "merge-base", "--is-ancestor", p.after, tip).returncode != 0:
                raise CannotRead(f"the push at {p.at:%Y-%m-%dT%H:%M:%SZ} delivered a commit the "
                                 "line being judged does not contain (main reset, or the ref not "
                                 "up to date)")


def cut_off_by_until(repo: Path, walk: str, ref: str, later: list["Push"]) -> None:
    """The commits of `ref` after the historical tip `walk` are left for the
    next run ONLY if a push after --until is shown to have delivered each of
    them (fail closed). A later push whose range this clone cannot read shows
    nothing either way (A→C may have delivered a B the clone holds), so a
    commit only such a push could account for is unverified: exit 2."""
    cut = _git(repo, "rev-list", f"{walk}..{ref}").split()
    if not cut:
        return
    delivered: set[str] = set()
    for p in later:
        if not p.after or p.after == ZERO:
            continue
        span = [p.after] + ([f"^{p.before}"] if p.before and p.before != ZERO else [])
        r = _gitrun(repo, "rev-list", *span)
        if r.returncode == 0:
            delivered.update(r.stdout.split())
    missing = [c for c in cut if c not in delivered]
    if missing:
        raise CannotRead(f"{len(missing)} commit(s) after main's tip at --until: delivery by a push "
                         "after it could not be verified")


def _blob(repo: Path, commit: str, path: str) -> bytes:
    """The file at `commit`: empty only when git confirms the path is not in
    that commit's tree; any read that fails raises (never taken as empty)."""
    listed = _gitrun(repo, "ls-tree", "--name-only", commit, "--", path)
    if listed.returncode != 0:
        raise CannotRead(f"git could not list {path} at {commit[:7]}")
    if not listed.stdout.strip():
        return b""                                     # confirmed: not in that commit's tree
    r = _gitrun(repo, "show", f"{commit}:{path}", text=False)
    if r.returncode != 0:
        raise CannotRead(f"git could not read {path} at {commit[:7]}")
    return r.stdout


def added_receipts(repo: Path, ref: str, slug: str, base: str | None) -> tuple[list[Added], list[str]]:
    """The receipts each first-parent commit of `ref` after `base` (main's tip
    at the window's start; None: the whole line) added to the market's file,
    oldest first; and the commits that could not be told (not append-only
    there, or a line that is not a JSON object)."""
    path = f"Data/{slug}/receipts.jsonl"
    span = [f"{base}..{ref}"] if base else [ref]
    commits = _git(repo, "log", "--first-parent", "--reverse", "--format=%H", *span, "--", path).split()
    out, untold = [], []
    for c in commits:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", c).split()[1:]
        before = _blob(repo, parents[0], path) if parents else b""
        now = _blob(repo, c, path)
        if not now.startswith(before):
            untold.append(c)
            continue
        for line in now[len(before):].splitlines():
            if line.strip():
                try:
                    receipt = json.loads(line)
                except ValueError:
                    receipt = None
                if isinstance(receipt, dict):
                    out.append(Added(slug, c, receipt))
                elif c not in untold:
                    untold.append(c)
    return out, untold


def delivered_by(repo: Path, pushes: list[Push], since: datetime) -> dict[str, datetime]:
    """commit -> the earliest push time that delivered it. A push inside the
    window whose range this clone cannot read raises: the commits it carried
    would otherwise take a later push's time. A push before the window cannot
    have delivered anything the walk covers (the walk starts after main's tip
    at the window's start), so an unreadable one there is passed over."""
    first: dict[str, datetime] = {}
    for p in sorted(pushes, key=lambda p: p.at):
        if not p.after or p.after == ZERO:
            continue
        span = [p.after] + ([f"^{p.before}"] if p.before and p.before != ZERO else [])
        r = _gitrun(repo, "rev-list", *span)
        if r.returncode != 0:
            if p.at >= since:
                raise CannotRead(f"git cannot read the commits of the push at {p.at:%Y-%m-%dT%H:%M:%SZ} "
                                 "(is this clone complete?)")
            continue
        for c in r.stdout.split():
            first.setdefault(c, p.at)
    return first


# --- GitHub, read-only ------------------------------------------------------

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


def main_pushes(repo: str, token: str | None, since: datetime, get=_get) -> tuple[list[Push], bool]:
    """Every event that moved main, newest first, back past `since`; and
    whether the history reaches back past `since` (False: it ended first, so
    older receipts may be CANNOT JUDGE). An answer that cannot show its own
    completeness raises CannotRead."""
    url = f"{API}repos/{repo}/activity?ref=refs/heads/main&per_page={PER_PAGE}"
    out: list[Push] = []
    for _ in range(MAX_PAGES):
        try:
            data, nxt = get(url, token)
        except (urllib.error.URLError, OSError, ValueError):
            raise CannotRead("the repository's push history cannot be read from GitHub") from None
        if not isinstance(data, list):
            raise CannotRead("GitHub's answer about the pushes is not a list")
        for item in data:
            try:
                out.append(Push(_at(item["timestamp"]), item.get("before") or "", item.get("after") or ""))
            except (KeyError, TypeError, AttributeError, ValueError):
                raise CannotRead("an entry of GitHub's push history cannot be read") from None
        if any(a.at < b.at for a, b in zip(out, out[1:])):
            raise CannotRead("GitHub's push history is not newest first, so its completeness cannot be shown")
        if out and out[-1].at < since:
            return out, True
        if nxt is None:
            if len(data) >= PER_PAGE:
                raise CannotRead("a full page of pushes with no next page: the history's end cannot be shown")
            return out, False
        if not nxt.startswith(API):
            raise CannotRead("GitHub's next page is not on its API")
        url = nxt
    raise CannotRead(f"more than {MAX_PAGES} pages of pushes")


# --- the verdict ------------------------------------------------------------

def deadline(receipt: dict, market) -> datetime:
    day = datetime.fromisoformat(receipt["basis_day"]).date()
    return datetime.combine(day, time(market.deadline_hour), tzinfo=market.tz).astimezone(timezone.utc)


SHAPES = {"target": re.compile(r"\d{4}-\d{2}-\d{2}"),
          "committed_at": re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(\+00:00|Z)")}


def shown(receipt: dict, field: str) -> str:
    """A date or a UTC time as printed: only in its known shape, else
    "<invalid>". A price or any other content never reaches the output."""
    v = receipt.get(field)
    return v if isinstance(v, str) and SHAPES[field].fullmatch(v) else "<invalid>"


def shown_strategy(receipt: dict, pairs: set[tuple[str, str]]) -> tuple[str, str]:
    """(strategy, version) as printed: only two strings forming a pair
    registered in the code (talea.loop.STRATEGIES of the checkout running
    this script), else ("<invalid>", "<invalid>")."""
    pair = (receipt.get("strategy"), receipt.get("strategy_version"))
    if all(isinstance(v, str) for v in pair) and pair in pairs:
        return pair
    return ("<invalid>", "<invalid>")


def judge(added: Added, market, pushed: dict[str, datetime]) -> tuple[str, datetime | None, datetime | None]:
    """(verdict, GitHub's push time or None, the deadline or None)."""
    try:
        gate = deadline(added.receipt, market)
    except (KeyError, TypeError, ValueError):
        return CANNOT_JUDGE, None, None
    at = pushed.get(added.commit)
    if at is None:
        return CANNOT_JUDGE, None, gate
    return (ON_TIME if at < gate else UNVERIFIABLE), at, gate


def window_base(pushes: list[Push], since: datetime) -> str | None:
    """Main's tip at the window's start, as GitHub recorded it: the `before`
    of the earliest event inside the window; with none inside, the `after` of
    the newest event before it; None when the record names no commit."""
    inside = [p for p in pushes if p.at >= since]
    tip = (inside[-1].before if inside else (pushes[0].after if pushes else ""))
    return tip if tip and tip != ZERO else None


def run(repo_dir: Path, repo: str, ref: str, now: datetime, days: int, markets: dict,
        token: str | None, get=_get, start: datetime | None = None,
        pairs: set[tuple[str, str]] | None = None, until: datetime | None = None) -> int:
    """`start`: receipts GitHub received before it are information, not the
    verdict. Nothing is selected or excused by a receipt's stamp."""
    since = now - timedelta(days=days)
    pairs = _strategies() if pairs is None else pairs
    try:
        pushes, reaches = main_pushes(repo, token, since, get=get)
        walk, later = ref, []
        if until is not None:            # main as GitHub recorded it then; later pushes: the next run
            later = [p for p in pushes if p.at > until]
            pushes = [p for p in pushes if p.at <= until]
            walk = next((p.after for p in pushes if p.after and p.after != ZERO), ref)
        check_clone(repo_dir, ref, pushes, since)
        if until is not None:            # FAIL CLOSED on both sides of the cut
            all_on(repo_dir, walk, pushes, since)        # a reset before --until, restored after it
            cut_off_by_until(repo_dir, walk, ref, later)
        pushed = delivered_by(repo_dir, pushes, since)
        base = window_base(pushes, since)
        found = {slug: added_receipts(repo_dir, walk, slug, base) for slug in sorted(markets)}
    except CannotRead as exc:
        print(f"[push-times] CANNOT JUDGE: {exc}")
        return 2
    print(f"[push-times] receipts whose commit reached main since {since:%Y-%m-%dT%H:%MZ} "
          f"(after {base[:7] if base else 'the start of the line'}); GitHub's push history "
          f"{'reaches back past it' if reaches else 'ends before it'}")
    if start is not None:
        print(f"[push-times] the verdict covers receipts GitHub received from {start:%Y-%m-%dT%H:%MZ} "
              "(and any with no push in the record); earlier ones are information")
    worst, notes = (0 if reaches else 2), []
    if not reaches:
        notes.append("CANNOT JUDGE: GitHub's push history ends before the window's start, so "
                     "the window cannot be shown complete")
    for slug, (added, untold) in found.items():
        counts = {ON_TIME: 0, UNVERIFIABLE: 0, CANNOT_JUDGE: 0}
        before = {ON_TIME: 0, UNVERIFIABLE: 0, CANNOT_JUDGE: 0}
        for c in untold:
            counts[CANNOT_JUDGE] += 1
            notes.append(f"{slug}: CANNOT JUDGE commit {c[:7]}: the file there does not begin "
                         "with the file at its first parent, or a line is not a JSON object")
        for a in added:
            verdict, at, gate = judge(a, markets[slug], pushed)
            earlier = start is not None and at is not None and at < start
            (before if earlier else counts)[verdict] += 1
            if verdict != ON_TIME:
                r = a.receipt
                strategy, version = shown_strategy(r, pairs)
                notes.append(f"{slug}: {verdict} {shown(r, 'target')} [{strategy} "
                             f"v{version}] stamped {shown(r, 'committed_at')}, "
                             f"pushed {at.isoformat() if at else 'not in the record'}, deadline "
                             f"{gate.isoformat() if gate else 'unknown'} (commit {a.commit[:7]})"
                             + (" (before the start date: information)" if earlier else ""))
        print(f"[push-times] {slug}: " + ", ".join(f"{k} {v}" for k, v in counts.items())
              + ("; before the start date: " + ", ".join(f"{k} {v}" for k, v in before.items())
                 if start is not None else "")
              + (" (no receipt in the window)" if not any(counts.values()) and not any(before.values())
                 else ""))
        if counts[UNVERIFIABLE]:
            worst = 1
        elif counts[CANNOT_JUDGE] and worst != 1:
            worst = 2
    for n in notes:
        print(f"[push-times] {n}")
    return worst


def _registry():
    sys.dont_write_bytecode = True          # read-only: importing the registry writes no __pycache__
    sys.path[:0] = [s for s in (str(ROOT / "src"),) if s not in sys.path]   # once, first
    from talea.markets import MARKETS
    return MARKETS


def _strategies() -> set[tuple[str, str]]:
    """The (strategy, version) pairs registered in the code."""
    _registry()                             # the same import path, no bytecode
    from talea.loop import STRATEGIES
    return {(s["strategy"], s["strategy_version"]) for s in STRATEGIES}


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="OWNER/NAME on GitHub (default: $GITHUB_REPOSITORY)")
    ap.add_argument("--ref", default="HEAD", help="the main line to walk (default: HEAD)")
    ap.add_argument("--days", type=int, default=7,
                    help="receipts on commits GitHub received in the last N days")
    ap.add_argument("--since", default=None,
                    help="start date (YYYY-MM-DD, UTC): only receipts GitHub received from it set the exit code")
    ap.add_argument("--until", default=None,
                    help="the time the clone was fetched (ISO, with a zone): later pushes are left for the next run")
    args = ap.parse_args(argv)
    if not args.repo:
        ap.error("--repo OWNER/NAME is needed (or GITHUB_REPOSITORY)")
    try:
        start = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                 if args.since else None)
    except ValueError:
        ap.error("--since must be a date, YYYY-MM-DD")
    try:
        until = _at(args.until) if args.until else None
    except ValueError:
        ap.error("--until must be an ISO time with a zone, e.g. 2026-11-17T18:23:05Z")
    return run(ROOT, args.repo, args.ref, now or datetime.now(timezone.utc), args.days,
               _registry(), os.environ.get("GITHUB_TOKEN"), start=start, until=until)


RESULTS = {0: "ON TIME", 1: "UNVERIFIABLE", 2: "CANNOT JUDGE"}


def entry(argv: list[str] | None = None) -> int:
    """main(), and the one line a caller can trust: "[push-times] result: <word>"
    is printed last whenever the check finished. Anything unexpected is exit 2
    with fixed words (Python's own exit for an uncaught exception is 1, which
    must mean UNVERIFIABLE and nothing else)."""
    try:
        rc = main(argv)
    except SystemExit:
        raise                               # argparse's own refusals (exit 2), unchanged
    except Exception as exc:  # noqa: BLE001
        print(f"[push-times] CANNOT JUDGE: unexpected error ({type(exc).__name__})")
        rc = 2
    print(f"[push-times] result: {RESULTS[rc]}")
    return rc


if __name__ == "__main__":
    sys.exit(entry())
