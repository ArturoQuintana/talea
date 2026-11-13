#!/usr/bin/env python3
"""Refuse a change that rewrites the audit trail.

Every `receipts.jsonl` and `ledger.jsonl` under `Data/` is append-only: a line,
once committed, is never edited, deleted, reordered or moved. The machine
writers only ever append. This check holds a pull request to the same rule:

    python3 -I -S check_append_only.py --repo . \
        --base refs/remotes/origin/main --head <the PR's tip>

THE GUARANTEE. It protects every line that is on main at the MERGE BASE of the
two commits. For each such file, the file at the head must still be at the same
path, must still be an ordinary file (mode 100644: not a symbolic link, not a
submodule link, not a directory, not executable), and must begin with exactly
the bytes it had at the merge base. Anything after those bytes is an append and
is accepted; so is a new file. A file the change touched or added must end with
a line ending. The comparison is against the merge base and never against the
tip of main, which gains lines several times a day that a branch cut earlier
simply does not have yet. `--base` is always main, whatever branch a pull
request targets: a branch built on another branch that holds a rewrite is
compared with main, not with that branch. It must be given as a full ref
(`refs/remotes/origin/main`) or a 40-character commit id: a short name such as
`origin/main` can be taken over by a tag of that name.

THE GUARANTEE HOLDS ONLY IF NOTHING FROM THE CHANGE RUNS BEFORE THE CHECK. A
file in the change that Python would import (a module beside this script, a
`sitecustomize`), or a build step, could end the process with exit 0. So CI
runs this as the first step after the checkout, before any install; with the
runner's own `python3 -I -S` (isolated, no site: only the standard library can
be imported); and from MAIN's copy of this file, written to a temporary folder,
not the copy in the change. A change to this script is therefore judged by the
script as it stands on main. Run under GitHub Actions without `-I -S` it
refuses to start.

It does NOT judge how a change arrived at lines it adds itself. Only the tip is
compared, so a branch that appends a line and alters it in a later commit is
accepted with the altered line: a branch's own commits are not the record until
they are merged. To keep that visible, a passing run prints how many files the
change appended to, and how many lines.

Exit 0: nothing rewritten. Exit 1: at least one file was (each is listed, by
path and line number, never by content). Exit 2: the check could not be made
(a base that is not a full ref or a commit id, an unknown commit, no common
history, a shallow clone, no guarded file found, git missing) -- which is a
failure, not a pass.

LIMITS that cannot be closed from inside a pull request's own CI:
- A pull request that edits `.github/workflows/ci.yml` can switch the check
  off: the workflow file that runs is the change's own. That file is a reserved
  path, and such a change needs the operator's go. (This script and its test
  are reserved too, although an edit to them no longer changes what judges the
  edit.)
- A commit message with `[skip ci]` means no run at all, and nothing on GitHub
  requires this check before a merge until the repository rule exists.
- Pushes that do not come through a pull request (the machine writers) are
  never seen by it.
- Whether an appended line is valid, or true, is not its business:
  `verify_ledger.py` re-derives every settlement.

Standard library only, Python 3.9 or later; needs `git` on the PATH.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

GUARDED_NAMES = ("receipts.jsonl", "ledger.jsonl")
GUARDED_ROOT = "Data"
REGULAR_FILE = "100644"
FULL_REF = re.compile(r"refs/[A-Za-z0-9._/-]+")
COMMIT_ID = re.compile(r"[0-9a-f]{40}")


class CannotCheck(Exception):
    """The comparison could not be made. Never treated as a pass."""


class Entry(NamedTuple):
    mode: str
    kind: str        # blob | tree (a directory) | commit (a submodule link)
    obj: str


class Result(NamedTuple):
    found: list[str]             # what was rewritten
    compared: int                # guarded files at the merge base(s)
    bases: list[str]
    appended: dict[str, int]     # path -> lines added to an existing file
    new: dict[str, int]          # path -> lines in a file the change adds


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # GIT_NO_REPLACE_OBJECTS: a `refs/replace/` entry can make git answer for
    # one commit with the content of another; this check reads the real objects.
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    except OSError as exc:
        raise CannotCheck(f"git could not be run ({type(exc).__name__})") from exc


def _git(repo: Path, *args: str) -> bytes:
    r = _run(repo, *args)
    if r.returncode != 0:
        why = r.stderr.decode(errors="replace").strip() or f"exit {r.returncode}"
        raise CannotCheck(f"git {' '.join(args)}: {why}")
    return r.stdout


def guarded_entries(repo: Path, commit: str) -> dict[str, Entry]:
    """{path: entry} of everything in `commit` named like a guarded file,
    whatever it is: a file, a symbolic link, a submodule link, or a directory
    (`-t` lists directories too; without it a folder named `receipts.jsonl`
    is invisible and only the files inside it are seen)."""
    entries: dict[str, Entry] = {}
    for line in _git(repo, "ls-tree", "-r", "-t", "-z", commit, "--", GUARDED_ROOT).split(b"\0"):
        if not line:
            continue
        meta, path = line.split(b"\t", 1)
        name = path.decode(errors="surrogateescape")
        if name.rsplit("/", 1)[-1] in GUARDED_NAMES:
            entries[name] = Entry(*meta.decode().split())
    return entries


def not_a_regular_file(entry: Entry) -> str | None:
    """None for an ordinary file; otherwise what the entry is."""
    if entry.kind == "tree":
        return "a directory"
    if entry.kind == "commit":
        return "a submodule link"
    if entry.mode == "120000":
        return "a symbolic link"
    if entry.mode != REGULAR_FILE:
        return f"a file of mode {entry.mode}, not {REGULAR_FILE}"
    return None


def resolve(repo: Path, name: str, *, what: str) -> str:
    """The commit id `name` stands for. Only an unambiguous name is accepted: a
    full ref or a 40-character commit id (and `HEAD` for the head). `main` or
    `origin/main` is refused: a tag pushed under that name would be taken for
    the branch, with only a warning."""
    if not (COMMIT_ID.fullmatch(name) or FULL_REF.fullmatch(name)
            or (what == "head" and name == "HEAD")):
        raise CannotCheck(
            f"the {what} must be a full ref (refs/remotes/origin/main) or a "
            f"40-character commit id, not {name!r}: a short name can be taken over "
            "by a tag of the same name")
    return _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                f"{name}^{{commit}}").decode().strip()


def merge_bases(repo: Path, base: str, head: str) -> list[str]:
    """Every merge base of the two commits (more than one after a criss-cross)."""
    r = _run(repo, "merge-base", "--all", base, head)
    found = r.stdout.decode().split()
    if r.returncode != 0 or not found:
        raise CannotCheck(f"no merge base between {base} and {head}: unrelated histories")
    return found


def rewrite(old: bytes, new: bytes) -> str | None:
    """None when `new` is `old` plus whole appended lines; otherwise what is wrong."""
    if new == old:
        return None
    if new.startswith(old):
        if old and not old.endswith(b"\n"):
            return ("its last line had no line ending at the merge base, so the "
                    "added bytes change that line")
        if not new.endswith(b"\n"):
            return "the bytes added at its end are not a whole line (no line ending)"
        return None
    was, now = old.splitlines(keepends=True), new.splitlines(keepends=True)
    for n, (a, b) in enumerate(zip(was, now), start=1):
        if a != b:
            return f"line {n} is not the line it was at the merge base"
    return (f"has {len(now)} line(s) where the merge base has {len(was)}: "
            f"line(s) {len(now) + 1} to {len(was)} are gone")


def compare(repo: Path, base: str, head: str) -> Result:
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise CannotCheck(
            "this is a shallow clone: with history cut off, git can name an older "
            "commit as the merge base. The check needs the full history (fetch-depth 0)")
    base, head = resolve(repo, base, what="base"), resolve(repo, head, what="head")
    bases = merge_bases(repo, base, head)
    at_head = guarded_entries(repo, head)
    found: list[str] = []
    at_base_paths: set[str] = set()
    appended: dict[str, int] = {}

    def note(path: str, why: str) -> None:
        if f"{path}: {why}" not in found:
            found.append(f"{path}: {why}")

    for mb in bases:
        for path, was in sorted(guarded_entries(repo, mb).items()):
            at_base_paths.add(path)
            odd = not_a_regular_file(was)
            if odd:
                note(path, f"is {odd} at the merge base, so it cannot be compared")
                continue
            now = at_head.get(path)
            if now is None:
                note(path, "is gone from its path (deleted or moved)")
                continue
            odd = not_a_regular_file(now)
            if odd:
                note(path, f"is no longer an ordinary file: it is now {odd}")
                continue
            if now.obj == was.obj:
                continue
            old = _git(repo, "cat-file", "blob", was.obj)
            new = _git(repo, "cat-file", "blob", now.obj)
            why = rewrite(old, new)
            if why:
                note(path, why)
            else:
                appended[path] = max(appended.get(path, 0),
                                     new.count(b"\n") - old.count(b"\n"))
    if not at_base_paths:
        raise CannotCheck(
            f"no {' or '.join(GUARDED_NAMES)} under {GUARDED_ROOT}/ at the merge base: "
            "the wrong repository or the wrong commits. Nothing was compared")

    new_files: dict[str, int] = {}
    for path, now in sorted(at_head.items()):
        if path in at_base_paths:
            continue
        odd = not_a_regular_file(now)
        if odd:
            note(path, f"is a new record that is not an ordinary file: it is {odd}")
            continue
        body = _git(repo, "cat-file", "blob", now.obj)
        if body and not body.endswith(b"\n"):
            note(path, "is new and its last line has no line ending")
        new_files[path] = body.count(b"\n")
    for path in [p for p in appended if any(f.startswith(f"{p}: ") for f in found)]:
        del appended[path]          # a rewrite relative to ANOTHER merge base
    return Result(found, len(at_base_paths), bases, appended, new_files)


def interpreter_problem(environ, flags) -> str | None:
    """Under GitHub Actions this must be run as `python3 -I -S`: isolated, so
    that no file of the change beside it can be imported in place of a standard
    module, and without `site`, so that no `sitecustomize` of the change runs
    first. Anywhere else (a developer's shell) it runs as it is started."""
    if environ.get("GITHUB_ACTIONS") == "true" and not (flags.isolated and flags.no_site):
        return ("under GitHub Actions this check must be started as "
                "`python3 -I -S`: otherwise a file in the change could be "
                "imported before it runs")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True,
                    help="main (origin/main in CI); its merge base with --head is compared")
    ap.add_argument("--head", default="HEAD", help="the tip of the change (default HEAD)")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                    help="the repository to read (default: the one this script is in)")
    args = ap.parse_args(argv)
    try:
        res = compare(Path(args.repo), args.base, args.head)
    except CannotCheck as exc:
        print(f"append-only: CANNOT CHECK -- {exc}")
        return 2
    at = ", ".join(b[:12] for b in res.bases)
    if res.found:
        print(f"append-only: REFUSED -- {len(res.found)} change(s) the audit trail "
              f"does not allow, against merge base {at}:")
        for line in res.found:
            print(f"  {line}")
        print("Receipts and ledgers are append-only (GOVERNANCE.md, invariant 2). "
              "Existing lines are never edited, deleted, reordered or moved, and "
              "every record is an ordinary file of whole lines.")
        return 1
    print(f"append-only: ok -- {res.compared} receipts/ledger file(s) at merge base {at}, "
          f"none rewritten")
    if not res.appended and not res.new:
        print("  this change adds no receipt or ledger line")
    else:
        print(f"  THIS CHANGE ADDS RECORD LINES: {len(res.appended)} file(s) appended to "
              f"(+{sum(res.appended.values())} line(s)), {len(res.new)} new file(s) "
              f"({sum(res.new.values())} line(s))")
        for path, n in sorted(res.appended.items()):
            print(f"    appended: {path} +{n} line(s)")
        for path, n in sorted(res.new.items()):
            print(f"    new: {path} {n} line(s)")
    return 0


if __name__ == "__main__":
    _problem = interpreter_problem(os.environ, sys.flags)
    if _problem:
        print(f"append-only: CANNOT CHECK -- {_problem}")
        sys.exit(2)
    print(f"append-only: python {sys.version.split()[0]}, isolated={sys.flags.isolated}, "
          f"no_site={sys.flags.no_site}, running {Path(__file__).resolve()}")
    sys.exit(main())
