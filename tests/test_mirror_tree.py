"""What the public mirror carries, and how the tests know where they are.

The public README tells a reader to run the suite from the mirror. That is only
true if (a) the mirror's file set is exactly the list in `scripts/mirror_tree.sh`
and nothing private, and (b) every test that needs a file outside that list is
skipped there by name, while in the private repository nothing skips at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

import conftest
from conftest import private_file, published_file

ROOT = Path(__file__).resolve().parents[1]
TREE = ROOT / "scripts" / "mirror_tree.sh"

# The whole list. A change to what the mirror carries is a PUBLIC change: it
# must change this, where a reader of the pull request will see it.
# Not Data/esios_prices.json: the deep history is not published (register T-36, D-26).
DATA_INCLUDES = {'--include="/$s/***"', "--include='/calibration/***'", "--include='/README-MOVED.md'"}
FOLDERS = ["Data/", "src/", "tests/", "scripts/"]
ROOT_FILES = {"VERIFY.md", "GOVERNANCE.md", "DATA-SOURCES.md", "pyproject.toml", "uv.lock",
              ".python-version", "LICENSE"}
DOCS = {"backtest-baselines-2015-2026.md", "backtest-markets-2026-08.md", "ARCHITECTURE.md",
        "gate-analysis-plan.md", "gate-verdict-2026-08.md", "incidents.md",
        "research-track-records-2026-08.md", "knowledge-map.md"}
RENAMED = {"README-public.md": "README.md", "mirror/verify.yml": ".github/workflows/verify.yml"}
NEVER = ("PRIVATE-REPO", "CLAUDE.md", "INFRA.md", "SECRETS.md", "open-decisions.md",
         "multiple-comparisons-policy.md", "BACKLOG.md", ".env",
         "team")    # the gate's code and the private-content check: never published


def _need(*programs: str) -> None:
    """These tests drive real programs (rsync, uv, git). In the PRIVATE
    repository a missing one is a FAILURE, never a skip: a check that skips
    when it cannot run has gone blind. On a public copy of these tests a
    reader without one of them is told so by a skip that names it."""
    missing = [name for name in programs if shutil.which(name) is None]
    if not missing:
        return
    message = f"needs {', '.join(missing)}, which is not installed or not on PATH"
    if conftest.is_private_repo():
        pytest.fail(message + ". In the private repository that is a failure, never a skip")
    pytest.skip(message)


@pytest.fixture
def _rsync_and_uv():
    _need("rsync", "uv")


@pytest.fixture
def _rsync():
    _need("rsync")


@pytest.fixture
def _git_and_rsync():
    _need("git", "rsync")


def test_in_the_private_repository_a_missing_program_fails_and_never_skips(monkeypatch):
    """The helper above, with git, rsync and uv all made to look missing. With
    the marker present it must FAIL; caught on purpose, because a skip raised
    here would mark THIS test as skipped and the thing it guards would pass
    unseen. With the marker made to look absent (a public copy) it skips and
    names what is missing."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(conftest, "is_private_repo", lambda: True)
    try:
        _need("git", "rsync")
    except pytest.skip.Exception:
        pytest.fail("in the private repository a missing program was skipped")
    except pytest.fail.Exception as e:
        assert "needs git, rsync" in str(e) and "never a skip" in str(e), str(e)
    else:
        pytest.fail("in the private repository a missing program passed")
    monkeypatch.setattr(conftest, "is_private_repo", lambda: False)
    with pytest.raises(pytest.skip.Exception, match=r"needs uv, which is not installed"):
        _need("uv")


def test_without_the_programs_the_tests_that_need_them_fail_and_none_is_skipped(tmp_path):
    """pytest itself, started on this file with a PATH that holds nothing, so
    no git, rsync or uv. Three tests that need them, one of each kind, must be
    reported FAILED with the message that says why, and none SKIPPED: a test
    whose decorator went back to `skipif(program missing)` would be reported
    skipped here, and this test fails on it."""
    if not conftest.is_private_repo():
        private_file("CLAUDE.md")                  # a public copy: skipped there by name
    nothing = tmp_path / "a-path-with-nothing-on-it"
    nothing.mkdir()
    chosen = ["test_the_built_tree_holds_the_list_and_nothing_private",
              "test_with_real_writers_nothing_is_written_through_a_link",
              "test_the_publisher_commits_and_pushes_the_mirrors_own_repository"]
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA",
         *[f"{Path(__file__).name}::{name}" for name in chosen]],
        cwd=Path(__file__).parent, capture_output=True, text=True, timeout=300,
        env={"PATH": str(nothing), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    out = r.stdout + r.stderr
    for name in chosen:
        assert f"FAILED test_mirror_tree.py::{name}" in out or f"ERROR test_mirror_tree.py::{name}" in out, out[-3000:]
    assert out.count("never a skip") >= len(chosen), out[-3000:]
    assert "SKIPPED" not in out and "skipped" not in out.splitlines()[-1], out[-1500:]


# mirror_tree.sh has two parts, divided by this line: first it judges the folder
# it was given (and only reads), then comes the list (and only the list copies).
DIVIDER = "# ---- the list ----"

# THE GUARD FOR THE TWO SCRIPTS THAT PUBLISH IS THEIR BYTES. Each is pinned whole,
# comments included, by sha256. An earlier version of this file tried to keep
# the part of mirror_tree.sh that judges the folder read-only with a list of
# forbidden words (rsync, cp, mkdir, `>` …), and a list of words loses to the
# next thing nobody thought of: the folder's variable reassigned after the
# check, an `eval`, a sourced file, a comment line that cuts a continued
# command short. So nothing is read for that purpose any more: one changed
# byte in either script and the test below fails and names the file.
# IF THE CHANGE IS INTENDED: update that file's line here, in the same pull
# request, and say in the pull request what changed and why. Both are reserved
# files: they decide what leaves the private repository, and where it is
# written.
# What this is NOT: a judgement of the change. It only makes a change
# impossible to miss; judging it is the readers' and the operator's work.
PINNED_SCRIPTS = {
    "scripts/mirror_tree.sh": "9464b4828bebe8fb62d8b36180d6b0a2913423a5251646448dab4860b4f97963",
    # (a comment line between the two, so that two pull requests changing one
    # line each can be merged by git)
    "scripts/publish_mirror.sh": "8bfd10e51c44bf175995cf73409498275c39477c81cfb02d3ac41cb4d6c892f8",
}

# THE READABLE STATEMENT of what is below the divider, comments and blank lines
# aside, word for word. The hashes above are the guard; this is so that a
# reader of a pull request sees WHAT changed in the list and not only that
# something did: no other exclude on a copy (an `--exclude='*.jsonl'` would
# silently stop publishing every receipt), no other command that writes into
# the folder, no change of order.
THE_LIST = r'''
cd "$REPO" || exit 2
export PATH="$HOME/.local/bin:$PATH"
SLUGS="$(uv run --no-dev python -m talea markets --public 2>/dev/null)"
if [ -z "$SLUGS" ]; then
  echo "[mirror] could not resolve public markets" >&2; exit 3
fi
ALLOW_OUT="$(timeout -k 30 120 uv run --no-project python -I -S team/publication_allowlist.py \
    --repo . --public "$SLUGS" 2>&1)"
ALLOWED=$?
[ -z "$ALLOW_OUT" ] || printf '%s\n' "$ALLOW_OUT" >&2
if [ "$ALLOWED" -ne 0 ]; then
  echo "[mirror] the publication allowlist did not pass (exit $ALLOWED): nothing is built" >&2; exit 3
fi
mkdir -p "$MIRROR" || exit 2
uv run --no-dev python scripts/render_dashboard.py --site "$MIRROR" >&2
mkdir -p "$MIRROR/docs"
INCLUDES=()
for s in $SLUGS; do INCLUDES+=(--include="/$s/***"); done
rsync -a --delete --delete-excluded \
  --exclude='__pycache__' --exclude='.tick.lock' \
  "${INCLUDES[@]}" \
  --include='/calibration/***' \
  --include='/README-MOVED.md' \
  --exclude='*' \
  Data/ "$MIRROR/Data/" >&2
rsync -a --delete --exclude __pycache__ src/ "$MIRROR/src/" >&2
rsync -a --delete --exclude __pycache__ tests/ "$MIRROR/tests/" >&2
rsync -a --delete --exclude __pycache__ scripts/ "$MIRROR/scripts/" >&2
cp VERIFY.md GOVERNANCE.md DATA-SOURCES.md pyproject.toml uv.lock .python-version "$MIRROR/"
[ -f LICENSE ] && cp LICENSE "$MIRROR/"
for d in backtest-baselines-2015-2026.md backtest-markets-2026-08.md ARCHITECTURE.md \
         gate-analysis-plan.md gate-verdict-2026-08.md incidents.md \
         research-track-records-2026-08.md knowledge-map.md; do
  cp "docs/$d" "$MIRROR/docs/" 2>/dev/null || true
done
cp README-public.md "$MIRROR/README.md"
mkdir -p "$MIRROR/.github/workflows"
cp mirror/verify.yml "$MIRROR/.github/workflows/verify.yml"
echo "$SLUGS"
'''


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _guard_and_list() -> tuple[str, str]:
    text = TREE.read_text()
    assert text.count(DIVIDER) == 1, f"mirror_tree.sh must hold the line {DIVIDER!r} exactly once"
    guard, the_list = text.split(DIVIDER)
    return _code(guard), _code(the_list)


@pytest.mark.parametrize("script", sorted(PINNED_SCRIPTS))
def test_the_two_scripts_that_publish_are_exactly_as_pinned(script):
    """The whole file, every byte, comments included. This is the guard for
    both scripts; see PINNED_SCRIPTS for why nothing less is."""
    now = hashlib.sha256((ROOT / script).read_bytes()).hexdigest()
    assert now == PINNED_SCRIPTS[script], (
        f"{script} changed: it is now sha256 {now}. It is a reserved file (it decides what "
        f"leaves the private repository and where it is written). If the change is intended, "
        f"update its line in PINNED_SCRIPTS in this same pull request, and say in the pull "
        f"request what changed and why")


def test_the_mirror_carries_exactly_this_list():
    """The readable statement, read from the one script that builds the
    mirror's file set. Every `rsync`, every `cp` and every `--include` in it is
    accounted for here; one more, or one fewer, fails. (The guard is the
    script's hash, above; this says in words what the list is.)"""
    _, code = _guard_and_list()
    assert [l for l in code.splitlines() if l.strip()] == THE_LIST.strip("\n").splitlines(), (
        "the list in mirror_tree.sh is no longer word for word the one pinned in THE_LIST: "
        "a change to what the mirror carries, or to how, is a public change and must be made in both")
    assert set(re.findall(r"--include=(?:\"[^\"]*\"|'[^']*')", code)) == DATA_INCLUDES
    assert "--exclude='*'" in code, "the Data copy must end by excluding everything not named"
    assert re.findall(r"^rsync .*?\s(\S+/) \"\$MIRROR/", code.replace("\\\n", " "), re.M) == FOLDERS
    copied: set[str] = set()
    for sources in re.findall(r'\bcp ((?:\S+ )+?)"\$MIRROR/', code):
        copied |= {w.strip('"') for w in sources.split()}
    docs = set(re.search(r"for d in (.*?); do", code.replace("\\\n", " "), re.S).group(1).split())
    assert docs == DOCS
    assert copied - {"docs/$d"} == ROOT_FILES | set(RENAMED), copied
    assert len(re.findall(r"^\s*(?:\[.*\] && )?(?:rsync|cp) ", code, re.M)) == 9, (
        "a copy was added to or removed from mirror_tree.sh: account for it above")
    # The one place the list may name team/: the publication allowlist's check
    # (register T-43) is RUN from there, never copied. Its two lines are word
    # for word in THE_LIST above; everything else must name none of NEVER.
    call = ('ALLOW_OUT="$(timeout -k 30 120 uv run --no-project python -I -S team/publication_allowlist.py \\\n'
            '    --repo . --public "$SLUGS" 2>&1)"\n')
    assert code.count(call) == 1, "the publication allowlist's check is not run exactly once, as pinned"
    for name in NEVER:
        assert name not in code.replace(call, ""), f"{name} must never be named in the mirror's list"


@pytest.mark.usefixtures("_rsync_and_uv")
def test_the_built_tree_holds_the_list_and_nothing_private(tmp_path):
    """The list above is text. This builds the tree the way the publisher and
    CI do and looks at what is actually in it. (Only the private repository can
    build it; on a public copy this test is skipped.)"""
    private_file("mirror/verify.yml")
    private_file("README-public.md")
    from talea.markets import MARKETS, public_markets

    tree = tmp_path / "mirror"
    # What an earlier publish could have left there and must not survive this
    # one: a market that is private now, a stray file beside the markets, the
    # deep history (published until T-36), and files since deleted from the
    # three code folders. Only the copies' own --delete and --delete-excluded
    # remove them.
    stale = ["Data/it/receipts.jsonl", "Data/it/prices.json", "Data/stray.json", "Data/es/gone.jsonl",
             "Data/esios_prices.json",
             "src/talea/gone.py", "tests/test_gone.py", "scripts/gone.sh"]
    for name in stale:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text("left by an earlier publish\n")
    r = subprocess.run(["bash", str(TREE), str(tree)], capture_output=True, text=True, cwd=ROOT,
                       timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    left = [name for name in stale if (tree / name).exists()]
    assert not left, f"still in the tree after the build: {left}"
    public = sorted(m.slug for m in public_markets())
    private = sorted(set(MARKETS) - set(public))
    assert r.stdout.split() == [m.slug for m in public_markets()], "stdout is the public slugs, nothing else"

    top = {p.name for p in tree.iterdir()}
    pages = {"index.html"} | {f"{s}.html" for s in public if not MARKETS[s].primary}
    assert top == {"Data", "src", "tests", "scripts", "docs", ".github", "README.md"} | ROOT_FILES | pages, top
    assert {p.name for p in (tree / "Data").iterdir()} == set(public) | {"calibration", "README-MOVED.md"}
    assert not (tree / "Data" / "esios_prices.json").exists(), "the deep history is not published (T-36)"
    assert {p.name for p in (tree / "docs").iterdir()} == DOCS, "every named document, and no other"
    for name in sorted(DOCS):
        assert (tree / "docs" / name).read_bytes() == (ROOT / "docs" / name).read_bytes(), name
    for name in sorted(ROOT_FILES):
        assert (tree / name).read_bytes() == (ROOT / name).read_bytes(), name
    assert (tree / ".github/workflows/verify.yml").read_bytes() == (ROOT / "mirror/verify.yml").read_bytes()

    def files(folder: Path, excluded: set[str]) -> set[str]:
        return {str(p.relative_to(folder)) for p in folder.rglob("*")
                if p.is_file() and not (excluded & set(p.relative_to(folder).parts))}
    # The folders the copies own hold EXACTLY the source's files: none dropped by
    # an exclude nobody listed, none added by a command nobody listed. Each
    # folder is compared leaving out what ITS copy excludes and nothing more:
    # the three code copies exclude __pycache__; the Data copy also .tick.lock.
    owned = {folder: {"__pycache__"} for folder in ("src", "tests", "scripts")}
    owned |= {f"Data/{s}": {"__pycache__", ".tick.lock"} for s in public + ["calibration"]}
    for folder, excluded in owned.items():
        here, there = files(tree / folder, excluded), files(ROOT / folder, excluded)
        assert here == there, (f"{folder}: only in the tree {sorted(here - there)[:5]}, "
                               f"only in the source {sorted(there - here)[:5]}")
    assert [str(p.relative_to(tree)) for p in sorted((tree / ".github").rglob("*")) if p.is_file()] == [
        ".github/workflows/verify.yml"]

    names = {p.name for p in tree.rglob("*")}
    for name in NEVER:
        assert name not in names, f"{name} is in the tree built for the public mirror"
    for slug in private:
        assert not (tree / "Data" / slug).exists(), f"private market {slug} is in the mirror's tree"
    assert (tree / "README.md").read_bytes() == (ROOT / "README-public.md").read_bytes()
    assert (tree / "tests" / "conftest.py").exists(), "the mirror needs the helper that skips by name"
    assert not any(p.name == "__pycache__" for p in tree.rglob("*"))

    # THE SERVER BUILDS INTO THE SAME FOLDER AT EVERY TICK. The builder refuses a
    # folder that holds a link or a file with a second hard link, so it must
    # never leave one behind itself: the tree it built holds none, and a second
    # build into it succeeds and changes nothing.
    odd = [str(p.relative_to(tree)) for p in tree.rglob("*")
           if p.is_symlink() or (p.is_file() and p.stat().st_nlink > 1)]
    assert not odd, f"the builder left a link or a multiply-linked file in its own tree: {odd[:5]}"
    def content(folder: Path) -> dict[str, bytes | None]:
        # The pages the renderer writes at the top carry a stamp to the minute, so
        # two builds in different minutes differ in them and in nothing else.
        # They are compared BY NAME; every other file byte for byte.
        return {str(p.relative_to(folder)): (None if p.parent == folder and p.name in pages else p.read_bytes())
                for p in folder.rglob("*") if p.is_file()}
    first = content(tree)
    assert {n for n, b in first.items() if b is None} == pages
    again = subprocess.run(["bash", str(TREE), str(tree)], capture_output=True, text=True, cwd=ROOT,
                           timeout=300)
    assert again.returncode == 0, "the second build into the same folder was refused or failed:\n" + again.stderr[-2000:]
    assert again.stdout == r.stdout
    assert content(tree) == first


# ---- where the tree may be built ----
#
# The copies delete whatever is in the folder and not on the list. Built into
# the repository itself that removes every private market's Data/ (seen on
# made-up folders: `rsync --delete --delete-excluded … --exclude='*' Data/ Data/`
# left only the public market) and overwrites README.md with the public one. So
# the script judges the folder first. These tests run the REAL script from a
# made-up checkout in a made-up home, with stand-ins for every program that
# writes: nothing here can touch a real folder even if the guard is broken.

def _snapshot(folder: Path) -> dict:
    seen = {}
    for p in sorted(folder.rglob("*")):
        mode = p.lstat().st_mode
        seen[str(p.relative_to(folder))] = (
            "link " + str(p.readlink()) if stat.S_ISLNK(mode) else
            "dir" if stat.S_ISDIR(mode) else
            p.read_bytes() if stat.S_ISREG(mode) else
            f"special {stat.S_IFMT(mode):o}")          # a pipe: never opened, reading one would not return
    return seen


# WHAT GITHUB RECEIVED IS COUNTED WHERE IT ARRIVES (Codex, rounds 4 to 7).
# The target: nothing but the one checked commit reaches GitHub, measured at
# the receiver. Out of scope (PO's ruling, round 7): a second push of the same
# commit to main (nothing changes at GitHub) and a push to "." (it never
# leaves the machine).
# Every bare origin the tests make (GitHub's stand-in) has a
# `reference-transaction` hook that appends each ref update it commits
# ("<origin> <old> <new> <ref>") to a log beside it: outside the mirror
# folder, the checkout and the publisher's environment, and whatever way git
# was started (an `env -i` push is recorded too). It sees every ref update,
# by push or not. Ref packing would show there too, so each origin's gc and
# maintenance are off. The hook is proven to log when it is installed (one
# throwaway ref, made and deleted, must show in the log, which is then
# emptied), and after every publish each origin's log and executable hook must
# still be there. After EVERY publish in these tests, both modes and every
# scenario, the fixture holds the publish to this:
#   - "[mirror] published [": exactly one update: refs/heads/main, from the
#     commit it was at to a new commit whose only parent is that one; no other
#     ref (no tag, no other branch), and main is now that commit;
#   - "[mirror] NOT KNOWN whether published": at most that one update;
#   - any other ending (refused, NOT published, up to date, quiet): no update.
# A second check, from the client side: the publisher runs with
# GIT_TRACE2_EVENT, so each git process that keeps the variable records its
# argv and the command it resolved to. Every push recorded there must be
# exactly `git -c push.followTags=false push -q --no-follow-tags <remote> <commit>:refs/heads/main`,
# where <remote> is the remote the mirror's branch tracks (branch.<b>.remote,
# read in the folder git says the push ran in) and its URL is the origin that
# received the update; there is exactly one when published (of the commit main
# received) and none when nothing was pushed. Whenever the publish reached the
# build (a program of the build or the gate was started: the market lookup,
# the renderer or the check), every ending from there on (a gate refusal, the
# check's signal ending, the destination, sync, staging, comparison, fetch,
# commit and push endings, published, up to date, not known) must leave a
# record that exists and holds that git ran: the repository check before the
# build asks git three questions. A missing record is then a failure, never "no push". Endings
# before the build are not held to this: some run no git that keeps the
# variable (the one git the publisher runs under `env -i`, listing git's
# repository variables, is never in the record; a stand-in git may answer).

ZERO = "0" * 40


def _record_what_it_receives(origin: Path) -> None:
    for key, value in (("gc.auto", "0"), ("maintenance.auto", "false"), ("receive.autogc", "false")):
        _git(origin, "config", key, value)
    hook = origin / "hooks" / "reference-transaction"
    hook.write_text(f'''#!/bin/sh
[ "$1" = committed ] || exit 0
while read -r old new ref; do printf '%s %s %s %s\\n' "$(pwd -P)" "$old" "$new" "$ref"; done >> "{origin}.received"
''')
    hook.chmod(0o755)
    log = Path(f"{origin}.received")
    _git(origin, "update-ref", "refs/liveness/probe", "refs/heads/main")
    _git(origin, "update-ref", "-d", "refs/liveness/probe")
    assert log.exists() and [l.split()[-1] for l in log.read_text().splitlines()] == ["refs/liveness/probe"] * 2, \
        "the origin's hook did not log a throwaway ref made and deleted"
    log.write_text("")


def _received(top: Path) -> dict[Path, str]:
    """Every origin's log of what it received, as it stands."""
    return {log: log.read_text() for log in sorted(top.rglob("*.received"))}


def _received_since(top: Path, before: dict[Path, str]) -> list[tuple[Path, str, str, str]]:
    """The ref updates the origins committed since `before`: (origin, old, new, ref)."""
    out, now = [], _received(top)
    for log in before:
        hook = log.with_name(log.name[:-len(".received")]) / "hooks" / "reference-transaction"
        assert log in now and hook.is_file() and os.access(hook, os.X_OK), \
            f"an origin's record of what it received is gone: {log}"
    for log, text in now.items():
        assert text.startswith(before.get(log, "")), f"{log} was rewritten"
        for line in text[len(before.get(log, "")):].splitlines():
            repo, old, new, ref = line.rsplit(" ", 3)
            out.append((Path(repo), old, new, ref))
    return out


def _git_ran(trace: Path) -> list[tuple[str, str, list[str], str | None]] | None:
    """Every git process recorded in `trace`, in the order each started:
    (its id, the command git resolved it to, its argv, its working tree);
    None if there is no record at all. A child's id starts with its parent's
    id and a slash."""
    if not trace.exists():
        return None
    argv, name, worktree, order = {}, {}, {}, []
    for line in trace.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "start":
            argv[event["sid"]] = event["argv"]
            order.append(event["sid"])
        elif event["event"] == "cmd_name":
            name[event["sid"]] = event["name"]
        elif event["event"] == "def_repo":
            worktree[event["sid"]] = event.get("worktree")
    return [(sid, name.get(sid, "?"), argv[sid], worktree.get(sid)) for sid in order]


ONE_PUSH = re.compile(r"([0-9a-f]{40}):refs/heads/main")
REACHED_GIT = ("[mirror] published [", "[mirror] up to date [", "[mirror] NOT KNOWN whether published", "nothing was pushed")


def _hold_the_pushes(stdout: str, received: list[tuple[Path, str, str, str]],
                     ran: list[tuple[str, str, list[str], str | None]] | None, reached_the_build: bool) -> list[list[str]]:
    """The publish held to the rules above: first what the origins received,
    then the client's record. Returns the pushes in that record (argv)."""
    for repo, old, new, ref in received:
        assert ref == "refs/heads/main" and old != ZERO and new != ZERO, \
            f"GitHub received an update other than main moving on: {old} {new} {ref}"
        assert _git(repo, "rev-parse", f"{new}^@").split() == [old], \
            f"main moved to a commit whose parents are not exactly the one it was at: {old} -> {new}"
    if "[mirror] published [" in stdout:
        assert len(received) == 1, f"published, and GitHub received {len(received)} updates: {received}"
        (repo, _, new, _), = received
        assert _git(repo, "rev-parse", "refs/heads/main").strip() == new
    elif "[mirror] NOT KNOWN whether published" in stdout:
        assert len(received) <= 1, f"GitHub received {len(received)} updates: {received}"
    else:
        assert received == [], f"not published, but GitHub received: {received}"
    if reached_the_build or any(ending in stdout for ending in REACHED_GIT):
        assert ran, "the publish reached the build, but git's record is missing or empty"
    pushes = [(wt, a) for _, n, a, wt in ran or [] if n == "push"]
    for wt, a in pushes:
        assert a[1:6] == ["-c", "push.followTags=false", "push", "-q", "--no-follow-tags"] \
            and len(a) == 8 and a[6] and ONE_PUSH.fullmatch(a[7]), f"a push of another form: {a}"
        assert wt, f"git's record does not say which folder the push ran in: {a}"
        folder = Path(wt)
        branch = _git(folder, "symbolic-ref", "-q", "--short", "HEAD").strip()
        tracked = _git(folder, "config", "--get", f"branch.{branch}.remote").strip()
        assert a[6] == tracked, f"a push to '{a[6]}', not to the remote the branch tracks ('{tracked}')"
        if received:
            url = _git(folder, "config", "--get", f"remote.{tracked}.url").strip()
            where = urllib.parse.urlsplit(url).path if "://" in url else url    # ssh://host/path: the path
            assert (folder / where).resolve() == received[0][0].resolve(), \
                f"the remote's URL {url} is not the origin that received the update"
    if "[mirror] published [" in stdout:
        assert [ONE_PUSH.fullmatch(a[7])[1] for _, a in pushes] == [received[0][2]], \
            f"git's record does not show one push of the commit main received: {[a for _, a in pushes]}"
    elif "[mirror] NOT KNOWN whether published" in stdout:
        assert len(pushes) <= 1, f"{len(pushes)} pushes: {[a for _, a in pushes]}"
    else:
        assert pushes == [], f"not published, but git pushed: {[a for _, a in pushes]}"
    return [a for _, a in pushes]


@pytest.fixture
def staged(tmp_path):
    home = tmp_path / "home"
    repo = home / "esios-paper"                # the checkout, directly under home as on the server
    for name in ("Data/es/receipts.jsonl", "Data/it/receipts.jsonl", "src/talea/loop.py",
                 "tests/test_x.py", "README.md", "PRIVATE-REPO"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(f"private copy of {name}\n")
    for name in sorted(ROOT_FILES | set(RENAMED)) + [f"docs/{d}" for d in sorted(DOCS)]:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(f"the checkout's {name}\n")
    (repo / "scripts").mkdir()
    shutil.copy(TREE, repo / "scripts" / "mirror_tree.sh")
    shutil.copy(ROOT / "scripts" / "publish_mirror.sh", repo / "scripts" / "publish_mirror.sh")
    other = home / "other-checkout"            # a second checkout of the private repository
    (other / "Data" / "it").mkdir(parents=True)
    (other / "PRIVATE-REPO").write_text("x\n")
    (home / "ledger-mirror" / ".git").mkdir(parents=True)   # the mirror's checkout, beside it
    (home / "ledger-mirror" / "README.md").write_text("the public mirror\n")
    (home / "locked").mkdir()                  # a folder nobody may enter (made so in the test)
    (home / "elsewhere").mkdir()
    (home / "elsewhere" / "a-file").write_text("not a folder\n")
    (home / "link-to-repo").symlink_to(repo)
    (home / "dangling").symlink_to(home / "nowhere")
    (repo / "link-out").symlink_to(home / "elsewhere")
    started = tmp_path / "started.log"
    # Stand-ins. `uv` is always one: it answers the market lookup with "es" (or
    # with nothing, when the test says the lookup fails) and, where the test
    # asks for real writing, plays the page renderer by writing index.html into
    # the folder it is given. It also plays the publisher's private-content
    # check (exit GATE_EXIT, default 0, printing GATE_SAYS), recording the
    # render time each of them was given, and the alert command: EVERY email
    # is kept, in order, as alerts/<n>.args (with the folder it was sent
    # from: the checkout, whose .env the real one reads) and alerts/<n>.body (exit
    # ALERT_EXIT; or exit 1 for the one numbered ALERT_FAILS_AT, e.g. 001 for
    # the second), and an email is not a program "started". rsync, cp and mkdir
    # are stand-ins that only record that they were started, unless the test
    # asks for the real ones.
    lookup_bin, stand_ins, alerts = tmp_path / "bin-uv", tmp_path / "bin-writers", tmp_path / "alerts"
    trace = tmp_path / "git-trace.jsonl"     # every git process a publish starts (above)
    lookup_bin.mkdir()
    stand_ins.mkdir()
    alerts.mkdir()
    (lookup_bin / "uv").write_text(f'''#!/bin/sh
case "$*" in *"-m talea alert"*)
  n=$(ls "{alerts}" | wc -l | tr -d ' '); n=$(printf '%03d' $((n / 2)))
  printf '%s\\n' "$*" "sent from $(pwd -P)" > "{alerts}/$n.args"; cat > "{alerts}/$n.body"
  [ "${{ALERT_FAILS_AT:-}}" != "$n" ] || exit 1
  exit "${{ALERT_EXIT:-0}}";;
esac
echo "uv $*" >> "{started}"
case "$*" in
  *"markets --public"*) [ -n "${{LOOKUP_FAILS:-}}" ] || echo es;;
  *"render_dashboard.py --site"*)
    echo "render at ${{TALEA_RENDER_EPOCH:-unset}}" >> "{started}"
    if [ -n "${{REAL_WRITES:-}}" ]; then for last; do :; done; printf 'the rendered page\\n' > "$last/index.html"; fi
    if [ -n "${{DURING_THE_BUILD:-}}" ]; then sh -c "$DURING_THE_BUILD"; fi;;
  *"publication_allowlist.py"*)
    for last; do :; done; echo "allowlist asked about: $last" >> "{started}"
    [ -z "${{ALLOWLIST_SAYS:-}}" ] || printf '%s\\n' "$ALLOWLIST_SAYS"
    exit "${{ALLOWLIST_EXIT:-0}}";;
  *"private_content_check.py"*)
    echo "check at ${{TALEA_RENDER_EPOCH:-unset}} in $(pwd -P)" >> "{started}"
    [ -z "${{GATE_SAYS:-}}" ] || printf '%s\\n' "$GATE_SAYS"
    if [ -n "${{GATE_SLEEP:-}}" ]; then : > "{tmp_path}/check-started"; sleep "$GATE_SLEEP"; fi
    exit "${{GATE_EXIT:-0}}";;
esac
''')
    (lookup_bin / "uv").chmod(0o755)
    # `timeout` too: it records how it was asked, then runs the command, unless
    # the test says the time ran out (exit 124, as GNU timeout): for the
    # private-content check with TIMEOUT_FIRES, for the publication allowlist's
    # check with ALLOWLIST_TIMEOUT_FIRES.
    (lookup_bin / "timeout").write_text(f'''#!/bin/sh
echo "timeout $1 $2 $3" >> "{started}"
case "$*" in
  *publication_allowlist.py*) [ -z "${{ALLOWLIST_TIMEOUT_FIRES:-}}" ] || exit 124;;
  *) [ -z "${{TIMEOUT_FIRES:-}}" ] || exit 124;;
esac
shift 3; exec "$@"
''')
    (lookup_bin / "timeout").chmod(0o755)
    # and `rsync`: the real one, except that the publisher's own sync of the
    # judged tree into the mirror (the one call with --exclude=/.git) can be
    # told to fail with RSYNC_SYNC_EXIT (127: as if rsync were missing), to
    # run without its --checksum (RSYNC_SYNC_QUICK), or to be preceded or
    # followed by a command (RSYNC_SYNC_FIRST, e.g. the mirror folder swapped
    # after its judgement; RSYNC_SYNC_THEN, e.g. a file changed after the sync).
    real_rsync = shutil.which("rsync") or "/usr/bin/rsync"
    (lookup_bin / "rsync").write_text(f'''#!/bin/sh
case "$*" in *"--exclude=/.git"*)
  [ -z "${{RSYNC_SYNC_EXIT:-}}" ] || exit "$RSYNC_SYNC_EXIT"
  [ -z "${{RSYNC_SYNC_FIRST:-}}" ] || sh -c "$RSYNC_SYNC_FIRST"
  if [ -n "${{RSYNC_SYNC_QUICK:-}}" ]; then
    for a; do shift; [ "$a" = --checksum ] || set -- "$@" "$a"; done
  fi
  "{real_rsync}" "$@" || exit $?
  [ -z "${{RSYNC_SYNC_THEN:-}}" ] || sh -c "$RSYNC_SYNC_THEN"
  exit 0;;
esac
exec "{real_rsync}" "$@"
''')
    (lookup_bin / "rsync").chmod(0o755)
    for name in ("rsync", "cp", "mkdir"):
        (stand_ins / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{started}"\n')
        (stand_ins / name).chmod(0o755)

    def run(folder, cwd=repo, script=repo / "scripts" / "mirror_tree.sh", home_set=True,
            real_writes=False, lookup_fails=False, judge_only=False, extra_env=None):
        if started.exists():
            started.unlink()
        before = _snapshot(home)
        path = f"{lookup_bin}:/usr/bin:/bin" if real_writes else f"{stand_ins}:{lookup_bin}:/usr/bin:/bin"
        env = {"PATH": path}
        if home_set:
            env["HOME"] = "" if home_set == "empty" else str(home)
        if real_writes:
            assert str(Path(folder).resolve()).startswith(str(tmp_path)), "real writes only under tmp_path"
            env["REAL_WRITES"] = "1"
        if lookup_fails:
            env["LOOKUP_FAILS"] = "1"
        env.update(extra_env or {})
        r = subprocess.run([shutil.which("bash"), str(script), *(["--judge-only"] if judge_only else []), str(folder)],
                           cwd=cwd, text=True,
                           capture_output=True, timeout=60,   # a guard that never answers is red
                           env=env)
        r.started = started.read_text().splitlines() if started.exists() else []
        r.untouched = _snapshot(home) == before
        return r

    def publish_env(mirror, during_the_build="", also_set=None, without=()):
        env = {"PATH": f"{lookup_bin}:{Path(shutil.which('git')).parent}:/usr/bin:/bin",
               "HOME": str(home), "MIRROR_DIR": str(mirror), "REAL_WRITES": "1",
               "MIRROR_BASE": "b" * 40,
               "DURING_THE_BUILD": during_the_build, **(also_set or {}),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TRACE2_EVENT": str(trace)}
        return {k: v for k, v in env.items() if k not in without}

    def publish(mirror, during_the_build: str = "", also_set: dict[str, str] | None = None,
                started_as: str | None = None, started_in: Path = home, without: tuple[str, ...] = ()):
        """The real publisher from the made-up checkout, with the real git, rsync,
        cp and mkdir, into `mirror`. Everything it can reach is under tmp_path.
        `during_the_build` is a command the stand-in renderer runs, for the tests
        in which something changes while the tree is built. `started_as` and
        `started_in` are the name it is started by and the folder it is started
        from, when a test needs those to be relative. `without` names variables
        left out of its environment altogether."""
        assert not os.path.isabs(str(mirror)) or str(mirror).startswith(str(tmp_path)), "real writes only under tmp_path"
        if started.exists():
            started.unlink()
        for old in alerts.iterdir():
            old.unlink()
        trace.unlink(missing_ok=True)
        received = _received(tmp_path)
        before = _snapshot(home)
        r = subprocess.run([shutil.which("bash"), started_as or str(repo / "scripts" / "publish_mirror.sh")],
                           cwd=started_in, text=True, capture_output=True, timeout=120,
                           env=publish_env(mirror, during_the_build, also_set, without))
        r.started = started.read_text().splitlines() if started.exists() else []
        r.untouched = _snapshot(home) == before
        r.git = _git_ran(trace)
        r.received = _received_since(tmp_path, received)
        r.pushes = _hold_the_pushes(r.stdout, r.received, r.git, reached_the_build=bool(r.started))
        return r

    run.home, run.repo, run.top, run.publish, run.publish_env = home, repo, tmp_path, publish, publish_env
    run.trace = trace
    run.received_now = lambda: _received(tmp_path)
    run.received_since = lambda before: _received_since(tmp_path, before)
    run.path_for_real_writes = f"{lookup_bin}:{Path(shutil.which('git') or '/usr/bin/git').parent}:/usr/bin:/bin"
    return run


REFUSED = [
    # (the folder given, the folder it is given from, what the refusal says)
    (".", "repo", "this repository, or inside it"),
    ("{repo}", "home", "this repository, or inside it"),
    ("{repo}/", "home", "this repository, or inside it"),
    ("scripts/..", "repo", "this repository, or inside it"),
    ("..", "repo-data", "this repository, or inside it"),
    ("{home}/link-to-repo", "home", "this repository, or inside it"),
    ("{home}/link-to-repo/Data", "home", "this repository, or inside it"),
    ("{home}/elsewhere/../esios-paper", "home", "this repository, or inside it"),
    ("Data", "repo", "this repository, or inside it"),
    ("Data/it", "repo", "this repository, or inside it"),
    ("out", "repo", "this repository, or inside it"),          # not there yet
    ("Data/new", "repo", "this repository, or inside it"),     # not there yet
    ("..", "repo", "the home folder"),
    ("{home}", "repo", "the home folder"),
    ("{top}", "repo", "contains this repository"),
    ("/", "repo", "the root folder"),
    ("//", "repo", "the root folder"),
    ("{home}/other-checkout", "repo", "is a checkout of the private repository"),
    ("{home}/other-checkout/Data", "repo", "is a checkout of the private repository"),
    ("{home}/other-checkout/new", "repo", "is a checkout of the private repository"),
    ("", "repo", "no folder given"),
    ("{home}/nowhere/deeper", "repo", "its parent folder does not exist"),
    ("{home}/elsewhere/a-file", "repo", "it exists and is not a folder"),
    ("{home}/dangling", "repo", "it exists and is not a folder"),
    ("{home}/nowhere/..", "repo", "its parent folder does not exist"),
]


@pytest.mark.parametrize("folder, given_from, why", REFUSED, ids=[repr(c[0]) + " from " + c[1] for c in REFUSED])
def test_the_script_refuses_a_folder_it_would_damage(staged, folder, given_from, why):
    """Exit 2, the reason on stderr, nothing on stdout (so the publisher
    publishes nothing), no program that writes even started, and every file
    under the made-up home exactly as it was: no folder created either."""
    places = {"repo": staged.repo, "home": staged.home, "top": staged.top}
    r = staged(folder.format(**places), cwd={**places, "repo-data": staged.repo / "Data"}[given_from])
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "refusing to build into" in r.stderr and why in r.stderr, r.stderr
    assert r.stdout == ""
    assert r.started == [], f"a program that writes was started: {r.started}"
    assert r.untouched, "something under the made-up home changed"


@pytest.mark.parametrize("folder, given_from, why", REFUSED, ids=[repr(c[0]) + " from " + c[1] for c in REFUSED])
def test_asked_only_to_judge_it_refuses_the_same_folders_the_same_way(staged, folder, given_from, why):
    """--judge-only is the same judgement (the publisher asks it about the real
    mirror folder before the sync), and it stops there: the same refusal, and
    nothing started or written."""
    places = {"repo": staged.repo, "home": staged.home, "top": staged.top}
    r = staged(folder.format(**places), cwd={**places, "repo-data": staged.repo / "Data"}[given_from], judge_only=True)
    assert r.returncode == 2 and "refusing to build into" in r.stderr and why in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


@pytest.mark.parametrize("folder", ["{home}/ledger-mirror", "{home}/elsewhere", "{home}/elsewhere/new"])
def test_asked_only_to_judge_a_folder_it_may_build_into_it_says_so_and_does_nothing(staged, folder):
    target = folder.format(home=staged.home)
    r = staged(target, judge_only=True)
    assert r.returncode == 0 and f"[mirror] '{target}' may be built into" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched
    real = staged(target, judge_only=True, real_writes=True)
    assert real.returncode == 0 and real.untouched and not (staged.home / "elsewhere" / "new").exists()


def test_the_same_folder_spelled_in_another_case_is_refused_where_that_is_the_same_folder(staged):
    """On a file system that ignores case (macOS by default) REPO and repo are
    one folder; the script compares folders by what they are on disk, not by
    spelling. Where case matters the two are different folders and this test
    has nothing to check: it then asserts only that."""
    shouted = staged.home / "ESIOS-PAPER"
    if not shouted.exists():
        assert [p.name for p in staged.home.iterdir() if p.name.lower() == "esios-paper"] == ["esios-paper"]
        return
    r = staged(shouted, cwd=staged.home)
    assert r.returncode == 2 and "this repository, or inside it" in r.stderr, (r.returncode, r.stderr)
    assert r.started == [] and r.untouched


def test_a_folder_that_cannot_be_entered_is_refused(staged):
    """The folder exists and `cd` into it fails, so where it really is cannot
    be established. Left unchecked the script would go on with an empty path,
    and every copy would be aimed at /Data/, /src/, … (Root may enter any
    folder; there the folder is simply a folder outside, and is accepted.)"""
    locked = staged.home / "locked"
    locked.chmod(0)
    try:
        r = staged(locked, cwd=staged.home)
    finally:
        locked.chmod(0o755)
    if os.geteuid() == 0:
        assert r.returncode == 0 and f"mkdir -p {locked.resolve()}" in r.started
        return
    assert r.returncode == 2 and "it could not be resolved to a folder" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


@pytest.mark.parametrize("home_set", [False, "empty"])
def test_without_a_home_folder_to_compare_with_it_refuses(staged, home_set):
    """The rule "never the home folder" cannot be applied when HOME is not set,
    or is set to nothing, so nothing is built. Exit 2 and the reason, not a
    shell error."""
    r = staged(staged.home / "elsewhere", home_set=home_set)
    assert r.returncode == 2 and "HOME is not set, or is empty" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


@pytest.mark.parametrize("folder, given_from", [
    (".", "repo"), ("Data", "repo"), ("out", "repo"), ("{home}/link-to-repo", "home"),
    ("{home}/link-to-repo/Data/new", "home"),
])
def test_this_repository_is_recognised_by_what_it_is_not_by_its_marker(staged, folder, given_from):
    """The checkout here has NO PRIVATE-REPO file (one cut before the marker
    existed, or one where it was deleted). The script still knows the folder it
    runs from: it compares folders by inode. In every other staged case the
    marker would have refused the folder anyway; here only this rule can."""
    (staged.repo / "PRIVATE-REPO").unlink()
    places = {"repo": staged.repo, "home": staged.home}
    r = staged(folder.format(**places), cwd=places[given_from])
    assert r.returncode == 2 and "it is this repository, or inside it" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


# ---- what the folder already holds ----

def _link(path: Path, to: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)               # in place of the file the folder had under that name
    path.symlink_to(to)


def _hard_link(path: Path, to: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    os.link(to, path)


INSIDE = [
    # (what is planted in an otherwise ordinary folder outside the repository, what the refusal names)
    ("Data", "link", "{repo}/Data", "it holds a link: Data"),
    ("src", "link", "{repo}/src", "it holds a link: src"),
    ("docs", "link", "{repo}/docs", "it holds a link: docs"),
    (".github", "link", "{repo}", "it holds a link: .github"),
    (".github/workflows", "link", "{repo}/scripts", "it holds a link: .github/workflows"),
    ("README.md", "link", "{repo}/README.md", "it holds a link: README.md"),
    ("index.html", "link", "{repo}/Data/it/receipts.jsonl", "it holds a link: index.html"),
    ("uv.lock", "link", "{repo}/uv.lock", "it holds a link: uv.lock"),
    ("docs/incidents.md", "link", "{repo}/docs/incidents.md", "it holds a link: docs/incidents.md"),
    (".github/workflows/verify.yml", "link", "{repo}/mirror/verify.yml",
     "it holds a link: .github/workflows/verify.yml"),
    ("Data/es", "link", "{repo}/Data/it", "it holds a link: Data/es"),
    ("scripts/deep/er/x.sh", "link", "{repo}/scripts/mirror_tree.sh", "it holds a link: scripts/deep/er/x.sh"),
    ("any-other-name", "link", "{home}/nowhere", "it holds a link: any-other-name"),      # dangling
    ("README.md", "hard link", "{repo}/README.md", "more than one hard link: README.md"),
    ("index.html", "hard link", "{repo}/Data/it/receipts.jsonl", "more than one hard link: index.html"),
    ("docs/incidents.md", "hard link", "{repo}/docs/incidents.md", "more than one hard link: docs/incidents.md"),
    ("tests/test_x.py", "hard link", "{repo}/tests/test_x.py", "more than one hard link: tests/test_x.py"),
]


@pytest.mark.parametrize("name, kind, leads_to, why", INSIDE, ids=[f"{c[0]} is a {c[1]}" for c in INSIDE])
def test_a_folder_outside_that_leads_back_inside_is_refused(staged, name, kind, leads_to, why):
    """The folder itself is fine: outside the repository, beside it. But one
    entry in it is a link, or a second hard link, to a file or folder of the
    private checkout, and a write aimed at that entry would land there. Refused
    before anything is started, whatever the entry's name and however deep."""
    target = staged.home / "ledger-mirror"
    to = Path(leads_to.format(repo=staged.repo, home=staged.home))
    (_link if kind == "link" else _hard_link)(target / name, to)
    r = staged(target)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "refusing to build into" in r.stderr and why in r.stderr, r.stderr
    assert r.stdout == "" and r.started == [] and r.untouched


def test_a_git_that_is_a_link_is_refused_like_any_other_link(staged):
    """Only a REAL `.git` folder is left unread. A `.git` that is a link leads
    into some other repository, and the publisher's `git add`, `commit` and
    `push` would follow it there."""
    target = staged.home / "elsewhere"
    _link(target / ".git", staged.home / "ledger-mirror" / ".git")
    r = staged(target)
    assert r.returncode == 2 and "it holds a link: .git" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


@pytest.mark.parametrize("name", ["README.md", "index.html", "docs/incidents.md", "scripts/deep/er/x.sh",
                                  "any-other-name"])
def test_a_pipe_in_the_folder_is_refused(staged, name):
    """A pipe (and, the same way, a socket or a device) is neither a folder nor
    a regular file. `cp` onto a pipe never returns: on the server that would
    hold the tick. Refused before anything is started."""
    target = staged.home / "ledger-mirror"
    (target / name).parent.mkdir(parents=True, exist_ok=True)
    (target / name).unlink(missing_ok=True)
    os.mkfifo(target / name)
    r = staged(target)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert f"it holds something that is neither a folder nor a regular file: {name}" in r.stderr, r.stderr
    assert r.stdout == "" and r.started == [] and r.untouched


def test_the_folders_own_git_folder_is_not_looked_into(staged):
    """A checkout's .git may hold links and hard-linked objects of its own. The
    builder never writes there, so they refuse nothing."""
    git = staged.home / "ledger-mirror" / ".git"
    _link(git / "a-link", staged.repo / "Data")
    (git / "objects").mkdir()
    (git / "objects" / "one").write_text("an object\n")
    os.link(git / "objects" / "one", git / "objects" / "two")
    r = staged(staged.home / "ledger-mirror")
    assert r.returncode == 0, r.stderr
    assert any(l.startswith("rsync ") for l in r.started)


def test_a_folder_whose_contents_cannot_be_read_is_refused(staged):
    """A sub-folder the script may not look into could hold a link. Not being
    able to look is a refusal, not a pass. (Root can read any folder; there the
    sub-folder is read, holds nothing, and the folder is accepted.)"""
    hidden = staged.home / "ledger-mirror" / "docs" / "hidden"
    hidden.mkdir(parents=True)
    hidden.chmod(0)
    try:
        r = staged(staged.home / "ledger-mirror")
    finally:
        hidden.chmod(0o755)
    if os.geteuid() == 0:
        assert r.returncode == 0
        return
    assert r.returncode == 2 and "what it holds could not be read" in r.stderr, (r.returncode, r.stderr)
    assert r.stdout == "" and r.started == [] and r.untouched


@pytest.mark.usefixtures("_rsync")
def test_with_real_writers_nothing_is_written_through_a_link(staged):
    """The same refusal, with the REAL rsync, cp and mkdir, and a stand-in page
    renderer that really writes index.html. Three entries of the mirror folder
    lead into the private checkout: Data (a link to its Data/), README.md (a
    link to its README) and index.html (a link to a private market's receipts).
    Without the refusal the Data copy deletes the private market behind the
    first, cp overwrites the README behind the second and the renderer
    overwrites the receipts behind the third; all of it here, in the made-up
    home. With it, every byte of the checkout is as it was."""
    target = staged.home / "ledger-mirror"
    _link(target / "Data", staged.repo / "Data")
    _link(target / "README.md", staged.repo / "README.md")
    _link(target / "index.html", staged.repo / "Data" / "it" / "receipts.jsonl")
    r = staged(target, real_writes=True)
    assert (staged.repo / "Data" / "it" / "receipts.jsonl").read_text() == "private copy of Data/it/receipts.jsonl\n", (
        "the private market's receipts were deleted or overwritten through a link in the mirror folder")
    assert (staged.repo / "README.md").read_text() == "private copy of README.md\n"
    assert r.untouched, "something under the made-up home changed"
    assert r.returncode == 2 and "it holds a link" in r.stderr, (r.returncode, r.stderr)
    assert r.started == [], "the lookup was started before the folder was judged"


@pytest.mark.usefixtures("_rsync")
def test_with_real_writers_an_ordinary_folder_gets_the_list_and_loses_what_is_stale(staged):
    """So that the test above cannot pass by never writing at all: the same
    real writers, an ordinary mirror folder. It ends up holding the public
    market and the listed files, with the checkout's bytes; what an earlier
    publish left there (a market private now, a stray file) is gone; and the
    private checkout is byte for byte as it was."""
    target = staged.home / "ledger-mirror"
    for name in ("Data/it/receipts.jsonl", "Data/stray.json", "src/gone.py"):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text("left by an earlier publish\n")
    before = _snapshot(staged.repo)
    r = staged(target, real_writes=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["es"]
    assert _snapshot(staged.repo) == before, "the build changed the checkout it reads from"
    assert (target / "Data/es/receipts.jsonl").read_text() == "private copy of Data/es/receipts.jsonl\n"
    assert sorted(p.name for p in (target / "Data").iterdir()) == ["es"], "only the public market"
    assert not (target / "src/gone.py").exists()
    assert (target / "src/talea/loop.py").exists() and (target / "tests/test_x.py").exists()
    assert (target / "README.md").read_text() == "the checkout's README-public.md\n"
    assert (target / "index.html").read_text() == "the rendered page\n"
    assert {p.name for p in (target / "docs").iterdir()} == DOCS
    assert (target / ".github/workflows/verify.yml").read_text() == "the checkout's mirror/verify.yml\n"
    assert not (target / "PRIVATE-REPO").exists()
    # and again into the same folder, as the server does at every tick
    built = _snapshot(target)
    again = staged(target, real_writes=True)
    assert again.returncode == 0, "the second build into the same folder was refused:\n" + again.stderr
    assert _snapshot(target) == built and _snapshot(staged.repo) == before


# ---- the publisher: its commit and push act on the mirror's own repository or on nothing ----

def _git(folder: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "HOME": str(folder), "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_CONFIG_GLOBAL": os.devnull})
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout


def _repository(folder: Path, origin: Path | None = None) -> None:
    """A made-up repository with one commit of whatever the folder holds, and,
    if asked, a bare origin beside it that the commit is pushed to."""
    folder.mkdir(parents=True, exist_ok=True)
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "config", "user.name", "made up")
    _git(folder, "config", "user.email", "made-up@example.test")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "--allow-empty", "-m", "as it was")
    if origin is not None:
        _git(folder.parent, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(folder, "remote", "add", "origin", str(origin))
        _git(folder, "push", "-q", "-u", "origin", "main")
        _record_what_it_receives(origin)


def _a_mirror(staged) -> tuple[Path, Path]:
    """A made-up mirror checkout with one commit, pushed to a bare origin beside it."""
    mirror, origin = staged.home / "the-mirror", staged.home / "the-mirror-origin.git"
    mirror.mkdir()
    (mirror / "README.md").write_text("the public mirror\n")
    _repository(mirror, origin)
    return mirror, origin


def _own_state(mirror: Path) -> tuple[str, bytes, str]:
    """What a publisher that refuses must leave exactly as it was IN THE MIRROR:
    its commits, its index byte for byte (so nothing is staged), its branches."""
    read = lambda *args: subprocess.run(["git", f"--git-dir={mirror / '.git'}", *args], capture_output=True, text=True,
                                        env={"PATH": os.environ["PATH"], "HOME": str(mirror), "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_CONFIG_GLOBAL": os.devnull}).stdout
    return read("rev-list", "--all"), (mirror / ".git" / "index").read_bytes(), read("for-each-ref")


# git and rsync: missing in the private repository is a failure (see `_need`).
needs_git_and_rsync = pytest.mark.usefixtures("_git_and_rsync")


@needs_git_and_rsync
def test_the_publisher_commits_and_pushes_the_mirrors_own_repository(staged):
    """The plain case, so that the two refusals below are not a publisher that
    never publishes: a real repository in the mirror folder, a bare origin, the
    real git. One new commit reaches origin, holding the public market and no
    private one; the private checkout is byte for byte as it was."""
    mirror, origin = _a_mirror(staged)
    before = _snapshot(staged.repo)
    r = staged.publish(mirror)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"
    assert _git(origin, "show", "main:Data/es/receipts.jsonl") == "private copy of Data/es/receipts.jsonl\n"
    names = _git(origin, "ls-tree", "-r", "--name-only", "main").split()
    assert "README.md" in names and not [n for n in names if n.startswith("Data/it") or n == "PRIVATE-REPO"]
    assert _snapshot(staged.repo) == before


# ---- the private-content gate (register D-37) ----
# The check itself is tested in team/tests; here, what the publisher does with
# its answer. The stand-in `uv` plays it (GATE_EXIT, GATE_SAYS, GATE_SLEEP) and
# the alert command (its arguments and body are kept); the stand-in `timeout`
# can run out (TIMEOUT_FIRES). The publisher ships in OBSERVE mode; ENFORCE is
# tested on a copy with that one line changed, as the pull request that turns
# it on will change it.

CHECK_SAYS = "Data/it/prices.json: bar 3 (line 7)\nprivate-content check: 3 finding(s)"


def _enforcing(staged) -> None:
    script = staged.repo / "scripts" / "publish_mirror.sh"
    text = script.read_text()
    assert text.count("\nGATE_MODE=observe\n") == 1
    script.write_text(text.replace("\nGATE_MODE=observe\n", "\nGATE_MODE=enforce\n"))


def _check_line(r) -> str:
    lines = [l for l in r.started if "private_content_check.py" in l and l.startswith("uv ")]
    assert len(lines) == 1, r.started
    return lines[0]


def _alerts(staged) -> list[tuple[str, str]]:
    """Every email the publisher sent, in order: (its arguments, its body).
    Each was sent from the checkout (the real alert reads its .env), wherever
    the publisher was by then."""
    folder = staged.top / "alerts"
    sent = [(a.read_text(), a.with_suffix(".body").read_text()) for a in sorted(folder.glob("*.args"))]
    for args, _ in sent:
        assert args.endswith(f"\nsent from {staged.repo.resolve()}\n"), args
    return sent


def _alert(staged) -> tuple[str, str]:
    """The one email the publisher sent, or ("", "") if it sent none. More than
    one is a failure here: the tests that expect two read `_alerts`."""
    sent = _alerts(staged)
    assert len(sent) <= 1, sent
    return sent[0] if sent else ("", "")


def _fixed_words(body: str) -> None:
    """The email names no path, no line of the check's own, no base, no folder."""
    for leak in ("Data/it", "bar 3", "line 7", "b" * 40, "/", "\\"):
        assert leak not in body, (leak, body)


def _not_published(staged, r, reason: str, subject: str | None = None) -> str:
    """The one ending that is not a publish: exit 1, the reason in the journal,
    and one email in fixed words that says so. Returns the email's body."""
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"[mirror] NOT published: {reason}" in r.stdout and "published [" not in r.stdout, r.stdout + r.stderr
    args, body = _alerts(staged)[-1]
    assert f"--subject talea mirror NOT published: {subject or reason} --body-file /dev/stdin" in args, args
    assert body.startswith("The public mirror was NOT published") and f"Reason: {reason}." in body, body
    assert "journalctl -u esios-tick" in body
    _fixed_words(body)
    return body


@needs_git_and_rsync
def test_the_gate_judges_a_fresh_build_against_the_ticks_base_and_then_publishes(staged):
    mirror, origin = _a_mirror(staged)
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    line = _check_line(r)
    tree = re.fullmatch(r"uv run --no-project python -I -S team/private_content_check\.py "
                        r"--repo \. --tree (\S+) --base (b{40})", line)
    assert tree and not tree.group(1).startswith(str(staged.home)), line     # a new folder, not the mirror
    assert "timeout -k 30 600" in r.started
    assert "[mirror] private-content check passed in " in r.stdout and "(mode observe" in r.stdout
    render, check = (next(l for l in r.started if l.startswith(w)) for w in ("render at ", "check at "))
    epoch = render.removeprefix("render at ")
    assert epoch.isdigit() and check == f"check at {epoch} in {staged.repo.resolve()}"
    assert r.started.index(render) < r.started.index(check)
    assert _alerts(staged) == [], "a publish that passed sent an email"


@needs_git_and_rsync
def test_an_inherited_render_time_is_never_used(staged):
    mirror, _ = _a_mirror(staged)
    r = staged.publish(mirror, also_set={"TALEA_RENDER_EPOCH": "1"})
    render = next(l for l in r.started if l.startswith("render at "))
    assert render != "render at 1" and int(render.split()[-1]) > 1_700_000_000


FAILS = [  # (what the stand-ins are told, what the reason must say)
    ({"GATE_EXIT": "1"}, "the check found 3 finding(s) (exit 1, after "),
    ({"GATE_EXIT": "2"}, "the check could not run (exit 2, after "),
    ({"GATE_EXIT": "3"}, "the check exited 3 after "),
    ({"GATE_EXIT": "127"}, "the check could not be started: uv or timeout is missing (exit 127, after "),
    ({"TIMEOUT_FIRES": "1"}, "the check did not finish within 600 s (exit 124, after "),
    ({"MIRROR_BASE": ""}, "no MIRROR_BASE, the trust anchor"),
]


@needs_git_and_rsync
@pytest.mark.parametrize("told, reason", FAILS)
def test_enforcing_any_fail_publishes_nothing_leaves_the_mirror_folder_alone_and_emails_fixed_words(
        staged, told, reason):
    _enforcing(staged)
    mirror, origin = _a_mirror(staged)
    before, folder = _own_state(mirror), _snapshot(mirror)
    r = staged.publish(mirror, also_set={"GATE_SAYS": CHECK_SAYS, **told})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "published [" not in r.stdout and f"REFUSED by the private-content gate: {reason}" in r.stdout
    assert _own_state(mirror) == before and _snapshot(mirror) == folder
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"
    args, body = _alert(staged)
    assert "--subject talea mirror NOT published: the private-content gate refused --body-file /dev/stdin" in args
    assert body.startswith("The public mirror was NOT published")
    assert f"Reason: the private-content gate refused: {reason}" in body
    assert "journalctl -u esios-tick" in body and "would have refused" not in body
    _fixed_words(body)
    if "MIRROR_BASE" in told:
        assert not [l for l in r.started if "private_content_check.py" in l]
    if "GATE_EXIT" in told:
        assert "bar 3 (line 7)" in r.stdout                     # the check's own lines: the journal only


@needs_git_and_rsync
@pytest.mark.parametrize("told, reason", FAILS)
def test_observing_any_fail_says_it_continues_and_then_that_it_published(staged, told, reason):
    """Two emails, each true when it is sent: at the check, that the gate
    would have refused and the publish goes on; after the push, that it was
    published. Never "published" before the push has succeeded."""
    mirror, origin = _a_mirror(staged)
    r = staged.publish(mirror, also_set={"GATE_SAYS": CHECK_SAYS, **told})
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert f"WOULD refuse (observe mode; continuing): {reason}" in r.stdout
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"
    (first_args, first), (last_args, last) = _alerts(staged)
    assert "--subject talea mirror: the private-content gate would have refused (observe mode; continuing) " in first_args
    assert first.startswith("The private-content gate would have refused this publish. It is in observe mode, "
                            "so the publish continues.")
    assert f"Reason: {reason}" in first and "published" not in first.replace("this publish", "")
    assert "--subject talea mirror published (observe mode) after the gate would have refused " in last_args
    assert last.startswith("The public mirror was published (observe mode)") and f"The gate's reason: {reason}" in last
    _fixed_words(first)
    _fixed_words(last)


@needs_git_and_rsync
@pytest.mark.parametrize("enforce", [True, False])
def test_a_gate_email_that_fails_is_said_and_changes_nothing_else(staged, enforce):
    if enforce:
        _enforcing(staged)
    mirror, origin = _a_mirror(staged)
    r = staged.publish(mirror, also_set={"GATE_EXIT": "1", "ALERT_EXIT": "1"})
    assert "the email could not be sent" in r.stdout
    assert r.returncode == (1 if enforce else 0)
    assert _git(origin, "rev-list", "--count", "main").strip() == ("1" if enforce else "2")


# ---- after the gate: the destination, the sync, what is staged, the push ----
# Each of these ends the publish in EITHER mode (they are not the gate's), so
# each is run in both. In observe mode after a gate that would have refused,
# the ending's email also says that the gate would have refused.

BOTH_MODES = pytest.mark.parametrize("enforce", [True, False], ids=["enforce", "observe"])


def _in_mode(staged, enforce: bool) -> None:
    if enforce:
        _enforcing(staged)


DESTINATIONS = [  # (the folder MIRROR_DIR names, what the builder's refusal says)
    ("{repo}", "this repository, or inside it"),
    ("{repo}/Data", "this repository, or inside it"),
    ("{home}/other-checkout", "is a checkout of the private repository"),
    ("{home}", "the home folder"),
]


@needs_git_and_rsync
@BOTH_MODES
@pytest.mark.parametrize("folder, why", DESTINATIONS, ids=[d[0] for d in DESTINATIONS])
def test_the_sync_never_goes_into_a_folder_the_builder_must_never_write_into(staged, folder, why, enforce):
    """MIRROR_DIR names the private checkout, a folder inside it, another
    checkout of the private repository, or home, and that folder is a git
    repository of its own (so the questions about the repository all answer
    "yes"). The sync would delete everything there that the public tree does
    not have. The builder's own judgement of the folder is asked about the
    real destination before the sync: it refuses, nothing is synced, staged or
    deleted, every file under home is as it was, and the operator is told."""
    _in_mode(staged, enforce)
    target = Path(folder.format(repo=staged.repo, home=staged.home))
    _repository(target)
    r = staged.publish(target)
    assert r.untouched, "something under home changed:\n" + r.stdout + r.stderr
    assert any(l.startswith("render at ") for l in r.started), "the tree was not built: this test proved nothing"
    assert f"refusing to build into '{target.resolve()}': " in r.stderr and why in r.stderr, r.stderr
    _not_published(staged, r, "the mirror folder is one the builder must never write into (mirror_tree.sh exit 2; "
                              "its reason is in the journal); nothing was synced")


@needs_git_and_rsync
def test_the_sync_goes_ahead_into_an_ordinary_mirror_after_the_builders_judgement(staged):
    """The judgement is asked, and it is the builder's own: its line is in the
    journal of an ordinary publish, between the gate and the sync."""
    mirror, _ = _a_mirror(staged)
    r = staged.publish(mirror)
    assert r.returncode == 0 and f"[mirror] '{mirror.resolve()}' may be built into" in r.stderr, r.stdout + r.stderr


@needs_git_and_rsync
def test_what_is_published_is_exactly_the_judged_build(staged):
    """A stray file in the mirror folder, a file the builder no longer makes,
    and a reader's .venv and .pytest_cache (which the check never reads) are
    all removed, never staged: the commit holds the judged tree and nothing
    else, so no later equality check can fail on them either."""
    mirror, origin = _a_mirror(staged)          # its commit holds README.md
    (mirror / "stray.txt").write_text("left by hand\n")
    (mirror / ".venv").mkdir(); (mirror / ".venv" / "pyvenv.cfg").write_text("x\n")
    (mirror / ".pytest_cache").mkdir(); (mirror / ".pytest_cache" / "v").write_text("x\n")
    r = staged.publish(mirror)
    assert "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    names = _git(origin, "ls-tree", "-r", "--name-only", "main").split()
    assert not [n for n in names if n.startswith((".venv", ".pytest_cache", "stray"))]
    assert not (mirror / "stray.txt").exists() and not (mirror / ".venv").exists()
    assert (mirror / ".git").is_dir() and _git(mirror, "status", "--porcelain") == ""


def _a_mirror_holding_other_bytes_of_the_same_size_and_time(staged) -> tuple[Path, Path, Path]:
    """A mirror whose last commit (and working tree) holds, for one file the
    build copies with its time kept, OTHER bytes of the SAME length, with that
    file's time set to the source's to the nanosecond. rsync's quick check
    (size and time) sees nothing to do there."""
    source = staged.repo / "Data/es/receipts.jsonl"
    mirror, origin = _a_mirror(staged)
    other = source.read_text().replace("private", "PRIVATE")
    assert len(other) == len(source.read_text()) and other != source.read_text()
    (mirror / "Data/es").mkdir(parents=True)
    (mirror / "Data/es/receipts.jsonl").write_text(other)
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-q", "-m", "other bytes")
    _git(mirror, "push", "-q")
    st = source.stat()
    os.utime(mirror / "Data/es/receipts.jsonl", ns=(st.st_atime_ns, st.st_mtime_ns))
    return mirror, origin, source


@needs_git_and_rsync
@BOTH_MODES
def test_a_file_of_the_same_size_and_time_but_other_bytes_is_replaced_by_the_judged_one(staged, enforce):
    """The regression: with `rsync -a` alone, the mirror's other bytes stayed,
    and were what the commit held. With --checksum the judged bytes replace
    them, and they are what reaches origin."""
    _in_mode(staged, enforce)
    mirror, origin, source = _a_mirror_holding_other_bytes_of_the_same_size_and_time(staged)
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "show", "main:Data/es/receipts.jsonl") == source.read_text()


DIFFERENCES = {  # what makes the staged tree differ from the judged one, after the sync
    "a sync by size and time only": lambda mirror: {"RSYNC_SYNC_QUICK": "1"},
    "a file changed after the sync": lambda mirror: {"RSYNC_SYNC_THEN": f"printf 'changed\\n' > '{mirror}/README.md'"},
    "a file added after the sync": lambda mirror: {"RSYNC_SYNC_THEN": f"printf 'new\\n' > '{mirror}/added.txt'"},
    "a file git is told is unchanged": lambda mirror: (
        _git(mirror, "update-index", "--assume-unchanged", "README.md"), {})[1],
    "a filter that rewrites content": lambda mirror: (
        _git(mirror, "config", "filter.shout.clean", "tr a-z A-Z"),
        (mirror / ".git" / "info").mkdir(exist_ok=True),
        (mirror / ".git" / "info" / "attributes").write_text("README.md filter=shout\n"), {})[3],
    "a file an ignore rule keeps out": lambda mirror: (
        (mirror / ".git" / "info").mkdir(exist_ok=True),
        (mirror / ".git" / "info" / "exclude").write_text("VERIFY.md\n"), {})[2],
}


@needs_git_and_rsync
@BOTH_MODES
@pytest.mark.parametrize("which", sorted(DIFFERENCES))
def test_nothing_is_committed_unless_what_is_staged_is_byte_for_byte_the_judged_tree(staged, which, enforce):
    """After `git add -A` the index is compared with the judged tree, path by
    path and blob by blob. Each case here makes them differ in a way the sync
    alone does not prevent: nothing is committed or pushed, and the operator
    is told."""
    _in_mode(staged, enforce)
    if which == "a sync by size and time only":
        mirror, origin, _ = _a_mirror_holding_other_bytes_of_the_same_size_and_time(staged)
    else:
        mirror, origin = _a_mirror(staged)
    commits = _git(origin, "rev-list", "--count", "main").strip()
    head = _git(mirror, "rev-parse", "HEAD")
    r = staged.publish(mirror, also_set=DIFFERENCES[which](mirror))
    _not_published(staged, r, "what git staged is not byte for byte the judged tree (the journal says where they differ)")
    assert _git(mirror, "rev-parse", "HEAD") == head, "something was committed"
    assert _git(origin, "rev-list", "--count", "main").strip() == commits, "something was pushed"


@needs_git_and_rsync
@BOTH_MODES
@pytest.mark.parametrize("code, reason", [("127", "rsync is missing (exit 127)"),
                                          ("23", "the built tree could not be put into the mirror folder (rsync exit 23)")])
def test_a_sync_that_fails_publishes_nothing_and_emails(staged, code, reason, enforce):
    """The sync is on the publish path in observe mode too: rsync missing or
    failing must be loud, never a quiet day without a publish."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    before = _own_state(mirror)
    r = staged.publish(mirror, also_set={"RSYNC_SYNC_EXIT": code})
    _not_published(staged, r, reason)
    assert len(_alerts(staged)) == 1 and _own_state(mirror) == before
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_the_sync_never_touches_the_mirrors_git_folder(staged):
    """--exclude=/.git with --delete: rsync leaves an excluded path alone, so a
    file under .git/ that the build does not have survives byte for byte, and
    the repository's settings with it."""
    mirror, origin = _a_mirror(staged)
    (mirror / ".git" / "stray-under-git").write_text("not the build's\n")
    config = (mirror / ".git" / "config").read_bytes()
    r = staged.publish(mirror)
    assert "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert (mirror / ".git" / "stray-under-git").read_text() == "not the build's\n"
    assert (mirror / ".git" / "config").read_bytes() == config


def _origin_main(origin: Path) -> str:
    return _git(origin, "rev-parse", "main").strip()


# ---- what is pushed: one commit of the compared tree, on GitHub's main ----

LEFTOVERS = {  # what an earlier publish left committed and unpushed in the mirror folder
    "a commit holding a stray file": False,
    "a commit holding a stray file, and one removing it again": True,
}


@needs_git_and_rsync
@BOTH_MODES
@pytest.mark.parametrize("which", sorted(LEFTOVERS))
def test_a_commit_an_earlier_publish_left_unpushed_is_never_pushed(staged, which, enforce):
    """The regression (round 2): an earlier publish committed and its push
    failed, and its commit holds a file this run's tree does not have. Pushing
    the branch would publish that file, although this run compared only its
    own tree; with a later commit that removes it, the branch's tip is clean
    and the file is still in its history. Only one commit is pushed: the
    compared tree, with GitHub's main as its only parent. Neither the file nor
    a leftover commit ever reaches origin."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    first = _origin_main(origin)
    (mirror / "stray-private.txt").write_text("never public\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-q", "-m", "left unpushed")
    leftovers = [_git(mirror, "rev-parse", "HEAD").strip()]
    if LEFTOVERS[which]:
        _git(mirror, "rm", "-q", "stray-private.txt")
        _git(mirror, "commit", "-q", "-m", "removed again")
        leftovers.append(_git(mirror, "rev-parse", "HEAD").strip())
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-parse", "main^@").split() == [first], "origin's main did not gain exactly one commit"
    assert _git(origin, "rev-parse", "main^{tree}") == _git(mirror, "write-tree"), "what was pushed is not the compared index"
    objects = _git(origin, "rev-list", "--all", "--objects")
    assert "stray-private.txt" not in objects and not [c for c in leftovers if c in objects]
    assert _git(mirror, "rev-parse", "HEAD").strip() == _origin_main(origin) and _git(mirror, "status", "--porcelain") == ""



@needs_git_and_rsync
@BOTH_MODES
def test_a_tag_in_the_mirror_folder_never_goes_out_with_the_push(staged, enforce):
    """Codex, round 3: with `push.followTags` set, git pushes, beside the ref
    asked for, every annotated tag that points into the pushed history and
    that origin lacks. Here an annotated tag (its message never judged) sits
    on GitHub's main, the commit the new one is made on, and the mirror's
    settings ask for followTags. The changed tree is published; neither the
    tag's ref nor its object reaches origin."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    _git(mirror, "tag", "-a", "unjudged", "-m", "a message nobody judged", "HEAD")
    tag = _git(mirror, "rev-parse", "unjudged").strip()
    _git(mirror, "config", "push.followTags", "true")
    first = _origin_main(origin)
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-parse", "main^@").split() == [first], "nothing new was pushed: this test proved nothing"
    assert _git(origin, "for-each-ref", "refs/tags") == "", "a tag reached origin"
    present = subprocess.run(["git", "-C", str(origin), "cat-file", "-e", tag], capture_output=True,
                             env={"PATH": os.environ["PATH"], "HOME": str(origin), "GIT_CONFIG_NOSYSTEM": "1",
                                  "GIT_CONFIG_GLOBAL": os.devnull})
    assert present.returncode != 0, "the tag's object reached origin"

@needs_git_and_rsync
def test_a_hook_in_the_mirror_cannot_change_or_stop_the_commit(staged):
    """The commit is made with `git commit-tree`, which runs no hook: a
    pre-commit hook that would fail, or would change the index, does neither."""
    mirror, origin = _a_mirror(staged)
    hook = mirror / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho hooked > hooked.txt && git add hooked.txt\nexit 1\n")
    hook.chmod(0o755)
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert "hooked.txt" not in _git(origin, "ls-tree", "-r", "--name-only", "main").split()


@needs_git_and_rsync
@BOTH_MODES
def test_a_commit_that_cannot_be_made_is_not_published(staged, enforce):
    """No author git may use (no email, and git told not to guess one)."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    _git(mirror, "config", "--unset", "user.email")
    _git(mirror, "config", "user.useConfigOnly", "true")
    head = _git(mirror, "rev-parse", "HEAD")
    r = staged.publish(mirror)
    _not_published(staged, r, "git could not commit the judged tree (git exit 128); nothing was pushed")
    assert _git(mirror, "rev-parse", "HEAD") == head
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
@BOTH_MODES
def test_when_githubs_main_cannot_be_fetched_nothing_is_pushed(staged, enforce):
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    _git(mirror, "remote", "set-url", "origin", str(staged.top / "no-such-origin.git"))
    head = _git(mirror, "rev-parse", "HEAD")
    r = staged.publish(mirror)
    _not_published(staged, r, "GitHub's main could not be fetched (git exit 128); nothing was pushed")
    assert _git(mirror, "rev-parse", "HEAD") == head
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


UNTRACKED = {  # how the mirror's branch fails to track main on a remote: (the change, what the journal says)
    "no upstream": (("branch", "-q", "--unset-upstream"), "tracks 'nothing' on 'nothing'"),
    "this repository itself (remote '.')": (("config", "branch.main.remote", "."), "tracks 'refs/heads/main' on '.'"),
    "another branch": (("config", "branch.main.merge", "refs/heads/gh-pages"), "tracks 'refs/heads/gh-pages' on 'origin'"),
}


@needs_git_and_rsync
@pytest.mark.parametrize("which", sorted(UNTRACKED))
def test_a_mirror_branch_that_does_not_track_main_pushes_nothing(staged, which):
    """With remote "." git fetches and pushes within the mirror folder's own
    repository: its main would move and the publisher say "published" while
    GitHub got nothing."""
    mirror, origin = _a_mirror(staged)
    change, says = UNTRACKED[which]
    _git(mirror, *change)
    refs = _git(mirror, "for-each-ref")
    r = staged.publish(mirror)
    assert f"[mirror] the mirror's branch main {says}" in r.stdout, r.stdout + r.stderr
    assert _git(mirror, "for-each-ref") == refs, "a branch moved"
    _not_published(staged, r, "the mirror's branch does not track main on GitHub; nothing was pushed")
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


# ---- a push that does not succeed: whether it reached GitHub is not known ----

NOT_KNOWN_MODES = pytest.mark.parametrize("mode", ["enforce", "observe", "observe after a would-refuse"])


def _in(staged, mode: str) -> dict[str, str]:
    if mode == "enforce":
        _enforcing(staged)
    return {"GATE_EXIT": "1", "GATE_SAYS": CHECK_SAYS} if mode == "observe after a would-refuse" else {}


def _not_known(staged, r, what: str, mode: str) -> str:
    """The ending after a push that did not succeed: exit 1, and one email
    that says whether anything reached GitHub is not known; never "NOT
    published", never "published"."""
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"[mirror] NOT KNOWN whether published: {what}" in r.stdout and "published [" not in r.stdout, r.stdout + r.stderr
    args, body = _alerts(staged)[-1]
    assert f"--subject talea mirror: not known whether published ({what}) --body-file /dev/stdin" in args, args
    assert body.startswith(f"Whether the public mirror was published is not known: {what}."), body
    assert "Whether anything reached GitHub is not known. The next publish retries" in body
    assert "NOT published" not in body and "nothing new was pushed" not in body
    assert "The public mirror was published" not in body and "already up to date" not in body
    repeated = "would have refused this publish (observe mode): the check found 3 finding(s)" in body
    assert repeated == (mode == "observe after a would-refuse"), body
    _fixed_words(body)
    return body


def _a_git_whose_push_reaches_github_and_then_fails(staged) -> str:
    """A PATH on which `git push` really pushes and then exits 128, as when the
    connection breaks after GitHub took the commit. Every other git command is
    the real git."""
    folder = staged.top / "a-push-that-breaks"
    folder.mkdir()
    real = shutil.which("git")
    (folder / "git").write_text(f'#!/bin/sh\ncase " $* " in *" push "*) "{real}" "$@"; exit 128;; esac\nexec "{real}" "$@"\n')
    (folder / "git").chmod(0o755)
    return f"{folder}:{staged.path_for_real_writes}"


@needs_git_and_rsync
@NOT_KNOWN_MODES
def test_a_push_that_reaches_github_and_then_fails_says_the_outcome_is_not_known(staged, mode):
    """The regression (round 2): a push that exits non-zero was reported as
    "nothing new was pushed". Here GitHub (the bare origin) takes the commit
    and the push still fails. The email says the outcome is not known; the
    next publish finds the judged tree on GitHub's main and pushes nothing."""
    told = _in(staged, mode)
    mirror, origin = _a_mirror(staged)
    r = staged.publish(mirror, also_set={**told, "PATH": _a_git_whose_push_reaches_github_and_then_fails(staged)})
    assert _git(origin, "rev-list", "--count", "main").strip() == "2", "this test proved nothing: the push did not arrive"
    _not_known(staged, r, "the push failed (git exit 128)", mode)
    again = staged.publish(mirror, also_set=told)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "[mirror] up to date [es] (GitHub's main already holds the judged tree; nothing was pushed)" in again.stdout
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"


@needs_git_and_rsync
@NOT_KNOWN_MODES
def test_a_push_github_refuses_is_not_known_and_the_next_publish_adds_one_commit(staged, mode):
    told = _in(staged, mode)
    mirror, origin = _a_mirror(staged)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    first = _origin_main(origin)
    r = staged.publish(mirror, also_set=told)
    _not_known(staged, r, "the push failed (git exit 1)", mode)
    assert _origin_main(origin) == first
    hook.unlink()
    again = staged.publish(mirror, also_set=told)
    assert again.returncode == 0 and "[mirror] published [es]" in again.stdout, again.stdout + again.stderr
    assert _git(origin, "rev-parse", "main^@").split() == [first]



@needs_git_and_rsync
@NOT_KNOWN_MODES
def test_a_signal_during_the_push_says_the_outcome_is_not_known(staged, mode):
    """TERM arrives while git pushes (here: the push reaches GitHub, then the
    publisher is signalled). The handler runs once the push returns: its
    email says the outcome is not known, never "NOT published"."""
    told = _in(staged, mode)
    mirror, origin = _a_mirror(staged)
    folder = staged.top / "a-push-then-a-signal"
    folder.mkdir()
    real = shutil.which("git")
    (folder / "git").write_text(f'#!/bin/sh\ncase " $* " in *" push "*) "{real}" "$@"; s=$?; kill -TERM $PPID; exit $s;; esac\n'
                                f'exec "{real}" "$@"\n')
    (folder / "git").chmod(0o755)
    r = staged.publish(mirror, also_set={**told, "PATH": f"{folder}:{staged.path_for_real_writes}"})
    _not_known(staged, r, "the publisher was stopped by a signal during the push", mode)
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"


# ---- the second email in observe mode ----

@needs_git_and_rsync
def test_observing_a_would_refuse_when_github_already_holds_the_tree_never_says_published(staged):
    """Round 2's other reader: after a would-refuse, a publish that pushes
    nothing new was reported as "published". It says "already up to date"."""
    mirror, origin = _a_mirror(staged)
    told = {"GATE_EXIT": "1", "GATE_SAYS": CHECK_SAYS}
    assert staged.publish(mirror, also_set=told).returncode == 0
    r = staged.publish(mirror, also_set=told)
    assert r.returncode == 0 and "published [" not in r.stdout, r.stdout + r.stderr
    assert "[mirror] up to date [es] (GitHub's main already holds the judged tree; nothing was pushed)" in r.stdout
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"
    (_, first), (last_args, last) = _alerts(staged)
    assert "would have refused (observe mode; continuing)" not in last_args
    assert "--subject talea mirror already up to date (observe mode) after the gate would have refused " in last_args
    assert last.startswith("The public mirror was already up to date (observe mode)"), last
    assert "The gate's reason: the check found 3 finding(s) (exit 1, after " in last
    assert "was published" not in last and "published (observe" not in last_args
    _fixed_words(last)


@needs_git_and_rsync
def test_when_the_second_email_cannot_be_sent_the_first_already_said_that_can_happen(staged):
    """The first email promises a second. It is not sent (the alert fails)
    although the publish succeeded: the journal says so, and the first email
    named that cause beside a publisher that stopped."""
    mirror, origin = _a_mirror(staged)
    r = staged.publish(mirror, also_set={"GATE_EXIT": "1", "GATE_SAYS": CHECK_SAYS, "ALERT_FAILS_AT": "001"})
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert "the email could not be sent" in r.stdout.split("[mirror] published [es]", 1)[1]
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"
    (_, first), _ = _alerts(staged)
    assert ("A second email says how the publish ended. If none arrives, either the publisher stopped before it "
            "could send it or that email could not be sent: the journal says which.") in first


# ---- the sync is bound to the folder that was judged ----

def _swap_for_a_link(folder: Path, to: Path) -> str:
    """A command that renames `folder` away (to <folder>.moved) and puts a
    link to `to` at its path."""
    return f"mv '{folder}' '{folder}.moved' && ln -s '{to}' '{folder}'"


def _swap_for_a_folder(folder: Path) -> str:
    """A command that renames `folder` away (to <folder>.moved) and puts a new,
    real folder at its path: no link anywhere, only another inode."""
    return f"mv '{folder}' '{folder}.moved' && mkdir '{folder}'"


@needs_git_and_rsync
def test_a_mirror_folder_replaced_by_a_link_before_its_judgement_is_refused(staged):
    mirror, origin = _a_mirror(staged)
    before, folder = _snapshot(staged.repo), _snapshot(mirror)
    r = staged.publish(mirror, during_the_build=_swap_for_a_link(mirror, staged.repo))
    _not_published(staged, r, "the mirror folder was moved or replaced before it was judged; nothing was synced")
    assert _snapshot(staged.repo) == before, "the private checkout changed"
    assert _snapshot(Path(f"{mirror}.moved")) == folder
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
@pytest.mark.parametrize("by", ["a link to the private checkout", "another folder"])
def test_a_mirror_folder_replaced_during_its_judgement_is_refused(staged, by):
    """Codex, round 2: the folder judged was found by its path, and rsync found
    it by its path again. Here the judgement passes and then the folder is
    renamed away and a link to the private checkout, or another folder, put at
    its path. The publisher is still in the folder it judged and sees the path
    is no longer that folder: nothing is synced, anywhere."""
    mirror, origin = _a_mirror(staged)
    scripts = staged.repo / "scripts"
    (scripts / "mirror_tree.sh").rename(scripts / "the_real_mirror_tree.sh")
    (scripts / "mirror_tree.sh").write_text(
        '#!/usr/bin/env bash\nbash "$(dirname "$0")/the_real_mirror_tree.sh" "$@"; s=$?\n'
        f'if [ "$1" = --judge-only ]; then '
        f'{_swap_for_a_link(mirror, staged.repo) if by.startswith("a link") else _swap_for_a_folder(mirror)}; fi\nexit $s\n')
    before, folder, resolved = _snapshot(staged.repo), _snapshot(mirror), mirror.resolve()
    r = staged.publish(mirror)
    assert f"[mirror] '{resolved}' may be built into" in r.stderr, "the judgement did not pass: this test proved nothing"
    _not_published(staged, r, "the mirror folder was moved or replaced while it was judged; nothing was synced")
    assert _snapshot(staged.repo) == before, "the private checkout changed"
    assert _snapshot(Path(f"{mirror}.moved")) == folder
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_a_mirror_folder_replaced_by_a_link_after_its_judgement_is_not_what_is_synced(staged):
    """The same swap just before rsync starts. rsync and git work in "." (the
    folder that was judged), never through the path: the private checkout is
    byte for byte as it was, and the judged folder is what is synced and
    pushed."""
    mirror, origin = _a_mirror(staged)
    before = _snapshot(staged.repo)
    r = staged.publish(mirror, also_set={"RSYNC_SYNC_FIRST": _swap_for_a_link(mirror, staged.repo)})
    assert (staged.home / "the-mirror.moved").is_dir() and mirror.is_symlink(), "the swap did not happen"
    assert _snapshot(staged.repo) == before, "the private checkout changed"
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert (staged.home / "the-mirror.moved" / "index.html").read_text() == "the rendered page\n"
    assert _git(origin, "show", "main:Data/es/receipts.jsonl") == "private copy of Data/es/receipts.jsonl\n"


@needs_git_and_rsync
@BOTH_MODES
def test_staging_that_fails_is_not_published(staged, enforce):
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    (mirror / ".git" / "index.lock").write_text("")      # another git is (as far as git knows) at work here
    r = staged.publish(mirror)
    _not_published(staged, r, "git could not stage the synced tree (git exit 128)")
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


def _a_path_where(staged, name: str, script: str) -> str:
    folder = staged.top / f"a-different-{name}"
    folder.mkdir()
    (folder / name).write_text(script)
    (folder / name).chmod(0o755)
    return f"{staged.top / 'bin-uv'}:{folder}:{staged.path_for_real_writes}"


# Every way the publication allowlist's check can fail to pass, as the stand-ins
# play it: a finding (1), a check that cannot run (2), no finish (124), and
# uv missing or unable to start the interpreter (127).
ALLOWLIST_FAILS = {"finds": {"ALLOWLIST_EXIT": "1"}, "cannot run": {"ALLOWLIST_EXIT": "2"},
                   "does not finish": {"ALLOWLIST_TIMEOUT_FIRES": "1"},
                   "cannot start": {"ALLOWLIST_EXIT": "127"}}
ALLOWLIST_EXITS = {"finds": 1, "cannot run": 2, "does not finish": 124, "cannot start": 127}


@pytest.mark.parametrize("what", sorted(ALLOWLIST_FAILS))
def test_a_check_that_does_not_pass_builds_nothing_and_exits_3(staged, what):
    """Fail-closed: whatever the allowlist's check does other than pass, the
    builder prints no slug, creates and writes nothing at the destination, and
    exits 3 (the publisher's "nothing was built"). Its line says the exit."""
    new = staged.home / "elsewhere" / "new"
    r = staged(str(new), extra_env={**ALLOWLIST_FAILS[what], "ALLOWLIST_SAYS": "publication allowlist: 1 finding(s)"})
    assert r.returncode == 3 and r.stdout == "", r.stdout + r.stderr
    code = ALLOWLIST_EXITS[what]
    assert f"[mirror] the publication allowlist did not pass (exit {code}): nothing is built" in r.stderr, r.stderr
    assert not new.exists() and r.untouched
    assert not [l for l in r.started if l.startswith(("mkdir ", "rsync ", "cp ")) or "render_dashboard" in l], r.started
    if what != "does not finish":
        assert "publication allowlist: 1 finding(s)" in r.stderr, "the check's own lines reach the journal"


def test_a_check_that_passes_lets_the_build_go_on(staged):
    new = staged.home / "elsewhere" / "new"
    r = staged(str(new), extra_env={"ALLOWLIST_SAYS": "publication allowlist: pass"})
    assert r.returncode == 0 and r.stdout.split() == ["es"], r.stderr
    assert "publication allowlist: pass" in r.stderr and "did not pass" not in r.stderr


BEFORE_THE_BUILD = {  # a failure before anything is built: (how, the reason)
    "no temporary folder": (lambda staged: {"PATH": _a_path_where(staged, "mktemp", "#!/bin/sh\nexit 1\n")},
                            "no temporary folder could be made for the build; nothing was built"),
    "no clock": (lambda staged: {"PATH": _a_path_where(
        staged, "date", '#!/bin/sh\n[ "$*" = "-u +%s" ] && exit 1\nexec /bin/date "$@"\n')},
                 "the clock could not be read for the render time; nothing was built"),
    "no public markets": (lambda staged: {"LOOKUP_FAILS": "1"},
                          "nothing was built (mirror_tree.sh exit 3; its reason is in the journal)"),
    # The publication allowlist (register T-43) is fail-closed in BOTH modes:
    # it is not the gate, and does not follow the gate's observe mode.
    **{f"the allowlist {what}": (lambda staged, how=how: how,
                                 "nothing was built (mirror_tree.sh exit 3; its reason is in the journal)")
       for what, how in ALLOWLIST_FAILS.items()},
}


@needs_git_and_rsync
@BOTH_MODES
@pytest.mark.parametrize("which", sorted(BEFORE_THE_BUILD))
def test_a_publish_that_cannot_build_is_not_published_and_says_so(staged, which, enforce):
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    state, folder = _own_state(mirror), _snapshot(mirror)
    how, reason = BEFORE_THE_BUILD[which]
    r = staged.publish(mirror, also_set=how(staged))
    _not_published(staged, r, reason)
    assert _own_state(mirror) == state and _snapshot(mirror) == folder
    assert not [l for l in r.started if "private_content_check.py" in l]


AFTER_A_WOULD_REFUSE = {  # observe mode: the gate would have refused, then this goes wrong
    "the sync fails": (lambda staged, mirror: {"RSYNC_SYNC_EXIT": "23"},
                       "the built tree could not be put into the mirror folder (rsync exit 23)"),
    "the fetch fails": (lambda staged, mirror: (
        _git(mirror, "remote", "set-url", "origin", str(staged.top / "no-such-origin.git")), {})[1],
                        "GitHub's main could not be fetched (git exit 128); nothing was pushed"),
    "the second asking fails": (lambda staged, mirror: {
        "DURING_THE_BUILD": f"git -C '{mirror}' config core.worktree '{staged.repo}'"},
        "the mirror folder is no longer its own git repository (the journal says which question failed); nothing was synced"),
}


@needs_git_and_rsync
@pytest.mark.parametrize("which", sorted(AFTER_A_WOULD_REFUSE))
def test_observing_a_fail_and_then_not_publishing_sends_two_true_emails(staged, which):
    """The first email says the gate would have refused and the publish goes
    on; the second, that it was NOT published, why, and that the gate would
    have refused. No email ever says "published"."""
    mirror, origin = _a_mirror(staged)
    how, reason = AFTER_A_WOULD_REFUSE[which]
    r = staged.publish(mirror, also_set={"GATE_EXIT": "1", "GATE_SAYS": CHECK_SAYS, **how(staged, mirror)})
    body = _not_published(staged, r, reason)
    (first_args, first), _ = _alerts(staged)
    assert "would have refused (observe mode; continuing)" in first_args
    assert ("Before that, the private-content gate would have refused this publish (observe mode): "
            "the check found 3 finding(s) (exit 1, after ") in body
    for _, sent in _alerts(staged):
        assert "was published" not in sent and "mirror published" not in sent
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
@BOTH_MODES
def test_github_receives_one_update_of_main_per_publish_and_none_otherwise(staged, enforce):
    """What the fixture holds after every publish, shown once on its own. The
    target: nothing but the one checked commit reaches GitHub, measured at the
    receiver (a second push of the same commit to main changes nothing there,
    and a push to "." never leaves the machine: both out of scope). A
    publish is ONE ref update at GitHub (the origin's own record): main, from
    where it was to the new commit, whose only parent is where it was; git's
    record on the publisher's side shows one push, of that commit, by its id,
    without tags. Publishing again with nothing new: no update, no push, and
    the fetch is in git's record. A publish the gate refuses (enforce): no
    update, no push."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    was = _origin_main(origin)
    r = staged.publish(mirror)
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    now = _origin_main(origin)
    assert r.received == [(origin.resolve(), was, now, "refs/heads/main")]
    assert [a[1:] for a in r.pushes] == [["-c", "push.followTags=false", "push", "-q", "--no-follow-tags",
                                          "origin", f"{now}:refs/heads/main"]]
    again = staged.publish(mirror)
    assert again.returncode == 0 and "[mirror] up to date [es]" in again.stdout, again.stdout + again.stderr
    assert again.received == [] and again.pushes == [] and "fetch" in [n for _, n, _, _ in again.git]
    if enforce:
        refused = staged.publish(mirror, also_set={"GATE_EXIT": "1", "GATE_SAYS": CHECK_SAYS})
        assert refused.returncode == 1 and "REFUSED by the private-content gate" in refused.stdout, refused.stdout
        assert refused.received == [] and refused.pushes == []


@needs_git_and_rsync
@BOTH_MODES
def test_a_signal_during_the_check_publishes_nothing_and_emails(staged, enforce):
    """systemd stops a unit by signalling its whole process group: the check
    ends, and the publisher sends its email and publishes nothing, in either
    mode. (A KILL cannot be caught: that one leaves the journal only.)"""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    received = staged.received_now()
    p = subprocess.Popen([shutil.which("bash"), str(staged.repo / "scripts" / "publish_mirror.sh")],
                         cwd=staged.home, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=staged.publish_env(mirror, also_set={"GATE_SLEEP": "30"}), start_new_session=True)
    marker = staged.top / "check-started"
    for _ in range(600):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), "the check never started"
    os.killpg(p.pid, signal.SIGTERM)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 1, out + err
    assert "published [" not in out and "stopped by a signal during the private-content check" in out
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"
    assert _hold_the_pushes(out, staged.received_since(received), _git_ran(staged.trace), reached_the_build=True) == []
    args, body = _alert(staged)
    assert "NOT published" in args and "Reason: the publisher was stopped by a signal during the private-content check." in body
    _fixed_words(body)


@needs_git_and_rsync
@BOTH_MODES
def test_a_signal_during_the_sync_publishes_nothing_and_emails(staged, enforce):
    """The trap covers every step, not only the gate: TERM while rsync runs
    (here: just after it, before the publisher goes on) ends the publish with
    its email, nothing committed."""
    _in_mode(staged, enforce)
    mirror, origin = _a_mirror(staged)
    head = _git(mirror, "rev-parse", "HEAD")
    r = staged.publish(mirror, also_set={"RSYNC_SYNC_THEN": "kill -TERM $(ps -o ppid= -p $PPID)"})
    _not_published(staged, r, "the publisher was stopped by a signal during the sync")
    assert _git(mirror, "rev-parse", "HEAD") == head
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


def test_the_mode_is_in_the_script_and_the_order_is_build_gate_ask_judge_sync_stage_compare_fetch_commit_push():
    raw = (ROOT / "scripts" / "publish_mirror.sh").read_text()
    code = _code(raw)
    assert re.findall(r"^GATE_MODE=.*$", code, re.M) == ["GATE_MODE=observe"]
    assert "${GATE_MODE:-" not in code and "GATE_MODE=\"$" not in code      # never from the environment
    order = [code.index(s) for s in (
        "trap stopped TERM INT HUP",
        "export TALEA_RENDER_EPOCH",
        'SLUGS="$(bash scripts/mirror_tree.sh "$TREE")"',
        "team/private_content_check.py",
        'cd -P -- "$MIRROR"',
        "mirror_is_its_own_repository . \\",
        'bash "$REPO/scripts/mirror_tree.sh" --judge-only "$MIRROR"',
        "the mirror folder was moved or replaced while it was judged",
        'rsync -a --checksum --delete --exclude=/.git "$TREE/" ./',
        "git add -A",
        "staged_is_the_judged_tree \\",
        'JUDGED_TREE="$(git write-tree)"',
        'git fetch -q "$REMOTE" refs/heads/main',
        'git commit-tree "$JUDGED_TREE" -p "$BASE"',
        'git -c push.followTags=false push -q --no-follow-tags "$REMOTE" "$NEW_COMMIT:refs/heads/main"',
        'echo "[mirror] published [$SLUGS]"',
    )]
    assert order == sorted(order), order
    # after entering the judged folder, nothing goes through its path again
    after = code[code.index('cd -P -- "$MIRROR"'):]
    assert code.index('in_the_judged_folder() { [ ! -L "$MIRROR" ] && [ . -ef "$MIRROR" ]; }') < code.index(after)
    assert [l for l in after.splitlines() if "$MIRROR" in l and "echo" not in l] == [
        'cd -P -- "$MIRROR" 2>/dev/null || fail "the mirror folder could not be entered; nothing was synced"',
        'bash "$REPO/scripts/mirror_tree.sh" --judge-only "$MIRROR"; JUDGED=$?']
    # How many pushes a publish makes, and of what, is held at run time from git's
    # own record, after every publish (_hold_the_pushes). Here only a cheap
    # backstop on the text: no push option that forces or sends more, and one
    # commit, by commit-tree
    assert not re.search(r"push.*(?:--force|\s-f\b|\s\+|--tags|--follow-tags|--mirror|--all)", code)
    assert not re.search(r"\bgit (?:-C \S+ )?commit\b(?!-tree)", code)
    # the trap is dropped right after the push has succeeded (Sonnet, round 3), and,
    # for an up-to-date publish, after the branch is set (and in `fail` and `not_known`)
    assert '|| not_known "the push failed (git exit $PUSHED)"\n  trap - TERM INT HUP' in code
    assert [m.start() for m in re.finditer(r"^trap - TERM INT HUP$", code, re.M)] == [code.index("\ntrap - TERM INT HUP\n") + 1]
    assert code.index("\ntrap - TERM INT HUP\n") > code.index('push -q --no-follow-tags "$REMOTE"')
    # the emails' words come from this script, never from the check, the base or a folder
    for name in ("send_alert", "common_lines", "fail", "not_known", "stopped", "gate_fails"):
        body = code.split(f"\n{name}() {{", 1)[1].split("\n}\n", 1)[0]
        for never in ("GATE_OUT", "MIRROR_BASE", "$MIRROR", "$TREE", "$BUILD_ROOT"):
            assert never not in body, (name, never)
    # every `exit` but the last two (published or up to date; the repository not found) is in `fail`, `not_known`
    exits = re.findall(r"\bexit [0-9]\b", code)
    assert exits.count("exit 0") == 1 and code.rstrip().endswith("exit 0")


@needs_git_and_rsync
def test_the_publisher_does_nothing_when_git_is_a_link_into_another_repository(staged):
    """The mirror folder's `.git` is a link to the private checkout's own git
    folder. `[ -d "$MIRROR/.git" ]` follows the link and is true. Left alone,
    `git add -A && git commit` in the mirror folder would write a commit into
    the PRIVATE repository. Nothing is built, nothing staged, and the operator
    is told (exit 1)."""
    _repository(staged.repo)
    mirror = staged.home / "the-mirror"
    mirror.mkdir()
    (mirror / ".git").symlink_to(staged.repo / ".git")
    head = _git(staged.repo, "rev-parse", "HEAD")
    r = staged.publish(mirror)
    assert "/.git is a link, not the mirror's own git folder" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "the mirror folder is not its own git repository (the journal says which question failed); nothing was built")
    assert r.started == [], f"the build was started: {r.started}"
    assert _git(staged.repo, "rev-parse", "HEAD") == head and r.untouched


@needs_git_and_rsync
def test_the_publisher_does_nothing_when_gits_working_tree_is_another_folder(staged):
    """The mirror folder holds a real `.git` whose `core.worktree` points at the
    private checkout. To git, the tree to stage is then the private checkout.
    The publisher asks git which folder its working tree is, and it is not the
    mirror folder: nothing is built, nothing staged, nothing pushed, and the
    operator is told (exit 1)."""
    mirror, origin = _a_mirror(staged)
    _git(mirror, "config", "core.worktree", str(staged.repo))
    state = _own_state(mirror)
    r = staged.publish(mirror)
    assert _own_state(mirror) == state, "the mirror's repository changed although publication was refused"
    assert "git's working tree for" in r.stdout and "is not that folder" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "the mirror folder is not its own git repository (the journal says which question failed); nothing was built")
    assert r.started == [], f"the build was started: {r.started}"
    assert _git(origin, "rev-list", "--count", "main").strip() == "1", "something was pushed"
    assert r.untouched


INHERITED = {
    "GIT_DIR and GIT_WORK_TREE": lambda repo, mirror: {"GIT_DIR": f"{repo}/.git", "GIT_WORK_TREE": str(mirror)},
    "GIT_INDEX_FILE": lambda repo, mirror: {"GIT_INDEX_FILE": f"{repo}/.git/index"},
    "GIT_COMMON_DIR": lambda repo, mirror: {"GIT_COMMON_DIR": f"{repo}/.git"},
    "GIT_OBJECT_DIRECTORY": lambda repo, mirror: {"GIT_OBJECT_DIRECTORY": f"{repo}/.git/objects"},
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": lambda repo, mirror: {"GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{repo}/.git/objects"},
    "GIT_NO_REPLACE_OBJECTS and GIT_PREFIX": lambda repo, mirror: {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_PREFIX": "Data/"},
}


@needs_git_and_rsync
@pytest.mark.parametrize("which", sorted(INHERITED))
def test_a_git_variable_inherited_from_the_tick_cannot_send_the_publish_elsewhere(staged, which):
    """The publisher is started with a variable that ties git to the PRIVATE
    repository (its git folder, its index, its common folder, its object
    store), as a hook or a wrapper could leave it set. Each is one of git's
    repository-local variables and is cleared before git is first used: the
    publish goes INTO THE MIRROR, and the private repository (its index, its
    objects, its branches, every file) is byte for byte as it was."""
    _repository(staged.repo)
    mirror, origin = _a_mirror(staged)
    before = _snapshot(staged.repo)
    r = staged.publish(mirror, also_set=INHERITED[which](staged.repo, mirror))
    assert _snapshot(staged.repo) == before, "the private repository was changed:\n" + r.stdout + r.stderr
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"
    assert _git(origin, "show", "main:Data/es/receipts.jsonl") == "private copy of Data/es/receipts.jsonl\n"
    assert not [n for n in _git(origin, "ls-tree", "-r", "--name-only", "main").split() if n.startswith("Data/it")]


def _a_git_that_answers_differently(staged, question: str, answer: str) -> str:
    """A PATH on which `git` is a stand-in that gives `answer` to one
    `rev-parse` question and hands everything else to the real git."""
    folder = staged.top / "a-different-git"
    folder.mkdir()
    (folder / "git").write_text(
        f'#!/bin/sh\ncase " $* " in *" {question} "*) printf \'%s\\n\' {answer}; exit 0;; esac\n'
        f'exec "{shutil.which("git")}" "$@"\n')
    (folder / "git").chmod(0o755)
    return f"{folder}:{staged.path_for_real_writes}"


@needs_git_and_rsync
def test_a_git_that_does_not_list_its_repository_variables_publishes_nothing(staged):
    """The variables to clear are git's own list. A git whose list lacks one of
    the five that matter most (here: an older list, without GIT_COMMON_DIR) has
    not told the publisher what to clear, and nothing is published."""
    mirror, origin = _a_mirror(staged)
    path = _a_git_that_answers_differently(staged, "--local-env-vars", "GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY")
    r = staged.publish(mirror, also_set={"PATH": path})
    assert "git did not list GIT_COMMON_DIR among its repository variables" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "git did not list the repository variables it must clear; nothing was built")
    assert r.started == [] and r.untouched
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_a_git_too_old_to_answer_a_question_publishes_nothing(staged):
    """SIMULATED: no old git was run. `rev-parse` prints back an option it does
    not know, so a git from before `--absolute-git-dir` existed would answer
    that question with the option's own name. Whatever the answer, it is not
    the mirror's `.git`: nothing is built and nothing published."""
    mirror, origin = _a_mirror(staged)
    path = _a_git_that_answers_differently(staged, "--absolute-git-dir", "--absolute-git-dir")
    r = staged.publish(mirror, also_set={"PATH": path})
    assert "git's folder for" in r.stdout and "is not its own .git (it says: '--absolute-git-dir')" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "the mirror folder is not its own git repository (the journal says which question failed); nothing was built")
    assert r.started == [] and r.untouched
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_an_inherited_namespace_cannot_send_the_push_somewhere_origins_main_is_not(staged):
    """GIT_NAMESPACE is not on git's own list of repository variables. With it
    inherited, the push lands under refs/namespaces/<name>/ on the mirror's
    origin: origin's main does not move, and the publisher still printed
    "published" (a reader ran it). It is cleared by name. The publish lands on
    origin's ordinary main, with the public receipt; no namespaced ref exists
    on origin; the private repository is byte-identical."""
    _repository(staged.repo)
    mirror, origin = _a_mirror(staged)
    before = _snapshot(staged.repo)
    r = staged.publish(mirror, also_set={"GIT_NAMESPACE": "elsewhere"})
    assert _snapshot(staged.repo) == before, "the private repository was changed:\n" + r.stdout + r.stderr
    assert r.returncode == 0 and "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "for-each-ref", "refs/namespaces") == "", "the push landed in a namespace"
    assert _git(origin, "rev-list", "--count", "main").strip() == "2", "origin's main did not move"
    assert _git(origin, "show", "main:Data/es/receipts.jsonl") == "private copy of Data/es/receipts.jsonl\n"


@needs_git_and_rsync
def test_the_variable_the_push_needs_for_its_key_survives(staged):
    """Not every GIT_ variable is cleared. The push may need GIT_SSH_COMMAND for
    the mirror's deploy key. Here the mirror's origin is an ssh address, and
    GIT_SSH_COMMAND names a stand-in that records being called and then serves
    the push locally: it is called, and the commit arrives."""
    mirror, origin = _a_mirror(staged)
    _git(mirror, "remote", "set-url", "origin", f"ssh://a-made-up-host{origin}")
    called = staged.top / "ssh-was-called.log"
    stand_in = staged.top / "stand-in-ssh"
    stand_in.write_text(f'#!/bin/sh\necho "$*" >> "{called}"\nfor last; do :; done\nexec sh -c "$last"\n')
    stand_in.chmod(0o755)
    r = staged.publish(mirror, also_set={"GIT_SSH_COMMAND": str(stand_in)})
    assert called.exists() and "a-made-up-host" in called.read_text(), "the push did not go through GIT_SSH_COMMAND:\n" + r.stdout + r.stderr
    assert "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"


@needs_git_and_rsync
def test_the_publisher_does_nothing_when_gits_common_folder_is_another_repositorys(staged):
    """A real `.git` in the mirror folder, holding a `commondir` file that names
    the PRIVATE repository's git folder. Git then takes the mirror's `.git` for
    a second working tree of the private repository: asked for its working tree
    and its own folder it answers "here", and its branches and objects are the
    private repository's. A commit made here would land on the private
    repository's main. Only the question about the common folder sees it."""
    _repository(staged.repo)
    mirror, origin = _a_mirror(staged)
    (mirror / ".git" / "commondir").write_text(f"{staged.repo}/.git\n")
    r = staged.publish(mirror)
    assert r.untouched, "something changed (the private repository's refs, index or objects, or the mirror):\n" + r.stdout + r.stderr
    assert "git's common folder for" in r.stdout and "is not its own .git" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "the mirror folder is not its own git repository (the journal says which question failed); nothing was built")
    assert r.started == [], f"the build was started: {r.started}"
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_a_search_path_for_cd_cannot_make_the_publisher_run_another_checkouts_scripts(staged):
    """The publisher is started by a relative name from the checkout, as a
    wrapper would start it, with CDPATH naming a folder that also holds a
    `scripts` folder. With CDPATH in force, `cd scripts/..` lands in THAT
    folder and its mirror_tree.sh is the one that runs. CDPATH is cleared
    first: the checkout's own builder runs, and the publish goes through."""
    mirror, origin = _a_mirror(staged)
    decoy = staged.home / "another-checkout"
    (decoy / "scripts").mkdir(parents=True)
    ran = staged.top / "the-decoy-ran"
    (decoy / "scripts" / "mirror_tree.sh").write_text(f'#!/bin/sh\necho ran > "{ran}"\n')
    r = staged.publish(mirror, started_as="scripts/publish_mirror.sh", started_in=staged.repo,
                       also_set={"CDPATH": str(decoy)})
    assert not ran.exists(), "another checkout's mirror_tree.sh was run:\n" + r.stdout + r.stderr
    assert "[mirror] published [es]" in r.stdout, r.stdout + r.stderr
    assert _git(origin, "rev-list", "--count", "main").strip() == "2"


@needs_git_and_rsync
def test_a_relative_mirror_folder_means_one_folder_whatever_cd_would_search(staged):
    """MIRROR_DIR is a bare name, and no folder of that name is where the name
    is resolved from (the checkout's root). A folder of that name does exist
    on CDPATH, and it is a repository with an origin. The name is resolved
    once, with CDPATH cleared, to nothing: nothing is built, the operator is
    told, and the folder on CDPATH and its origin are exactly as they were."""
    elsewhere = staged.home / "found-by-cdpath"
    competing, origin = elsewhere / "the-mirror", elsewhere / "the-mirror-origin.git"
    competing.mkdir(parents=True)
    (competing / "README.md").write_text("some other repository\n")
    _repository(competing, origin)
    state = _own_state(competing)
    r = staged.publish("the-mirror", also_set={"CDPATH": str(elsewhere)})
    assert "[mirror] the-mirror not initialized (no such folder)" in r.stdout, r.stdout + r.stderr
    _not_published(staged, r, "the mirror folder does not exist; nothing was built")
    assert r.started == [] and r.untouched and _own_state(competing) == state
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_the_publisher_asks_again_after_the_build_and_before_staging(staged):
    """The repository is the mirror's own when the publisher starts, and its
    working tree is pointed at the private checkout WHILE the tree is being
    built. The publisher asks git a second time before `git add`: the tree is
    built, nothing is synced, staged or pushed, and the operator is told."""
    mirror, origin = _a_mirror(staged)
    state = _own_state(mirror)
    r = staged.publish(mirror, during_the_build=f"git -C '{mirror}' config core.worktree '{staged.repo}'")
    assert any(l.startswith("render at ") for l in r.started), "the tree was not built: this test proved nothing"
    assert not (mirror / "Data").exists(), "the built tree reached the mirror folder before the second asking"
    _not_published(staged, r, "the mirror folder is no longer its own git repository (the journal says which "
                              "question failed); nothing was synced")
    # The MIRROR's own repository is as it was: no commit, no branch moved, and
    # its index byte for byte, so nothing was staged before the second asking.
    assert _own_state(mirror) == state, "the mirror's repository changed although publication was refused"
    assert _git(origin, "rev-list", "--count", "main").strip() == "1", "something was pushed"
    assert (staged.repo / "Data/it/receipts.jsonl").read_text() == "private copy of Data/it/receipts.jsonl\n"


def test_when_the_markets_cannot_be_resolved_nothing_is_created(staged):
    """Exit 3, "having created and written nothing at the destination": not
    even the folder. The lookup is the only thing started. (The lookup itself
    is `uv run` in the source repository, which may create its .venv there.)"""
    new = staged.home / "not-there-yet"
    r = staged(new, lookup_fails=True)
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "could not resolve public markets" in r.stderr and r.stdout == ""
    assert len(r.started) == 1 and r.started[0].startswith("uv ") and "markets --public" in r.started[0], r.started
    assert not new.exists() and r.untouched
    real = staged(new, lookup_fails=True, real_writes=True)      # and with the real mkdir
    assert real.returncode == 3 and not new.exists() and real.untouched


@pytest.mark.parametrize("folder, given_from", [
    # THE SERVER'S LAYOUT, by name: the mirror's checkout ~/ledger-mirror, an
    # existing git checkout directly under home, beside the checkout ~/esios-paper
    ("{home}/ledger-mirror", "repo"),
    ("{home}/elsewhere", "repo"),            # a folder that exists, outside
    ("{home}/elsewhere/new", "repo"),        # not there yet, its parent is
    ("../elsewhere", "repo"),
    ("link-out", "repo"),                    # a link kept in the repository that leads outside it
    ("{home}/mirror", "home"),               # beside the repository, not there yet
])
def test_the_script_builds_into_a_folder_outside(staged, folder, given_from):
    """The guard must not refuse everything. Here the stand-ins record what the
    script went on to start: the lookup, the publication allowlist's check, the
    render and the copies, every one of them aimed at the folder as it is on
    disk."""
    places = {"repo": staged.repo, "home": staged.home}
    r = staged(folder.format(**places), cwd=places[given_from])
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["es"]
    where = {"{home}/ledger-mirror": "ledger-mirror", "{home}/elsewhere": "elsewhere",
             "{home}/elsewhere/new": "elsewhere/new", "../elsewhere": "elsewhere",
             "link-out": "elsewhere", "{home}/mirror": "mirror"}[folder]
    target = str((staged.home / where).resolve())
    assert r.started[0].startswith("uv ") and "markets --public" in r.started[0], "the lookup comes first"
    assert r.started[1:4] == [
        "timeout -k 30 120",
        "uv run --no-project python -I -S team/publication_allowlist.py --repo . --public es",
        "allowlist asked about: es"], \
        "then the publication allowlist judges the slugs just resolved, under the gate's interpreter and a timeout"
    assert r.started[4] == f"mkdir -p {target}", "and only then is the folder created"
    copies = [l for l in r.started if l.startswith(("rsync ", "cp "))]
    assert len(copies) >= 9 and all(f" {target}/" in l for l in copies), copies


def test_the_publisher_says_why_nothing_was_built():
    """When the builder refuses or cannot resolve the markets, the publisher's
    line (and its email) must not blame the market lookup for a refusal: it
    gives the builder's exit code and points at its reason."""
    publish = _code((ROOT / "scripts" / "publish_mirror.sh").read_text())
    assert 'SLUGS="$(bash scripts/mirror_tree.sh "$TREE")"; BUILT=$?' in publish
    assert "mirror_tree.sh exit $BUILT" in publish


# ---- which repository is this ----

def test_this_copy_is_wholly_one_or_the_other():
    """In the private repository the marker AND the constitution are both here;
    on a public copy neither is. One without the other is a broken checkout:
    a deleted CLAUDE.md in the private repository fails here, by name, and a
    deleted marker fails here too, before it can turn failures into skips."""
    marker, claude = conftest.MARKER.exists(), (ROOT / "CLAUDE.md").exists()
    assert marker == claude, (
        f"PRIVATE-REPO is {'present' if marker else 'missing'} and CLAUDE.md is "
        f"{'present' if claude else 'missing'}: they must be both here (the private "
        "repository) or both absent (a public copy)")
    if marker:
        text = conftest.MARKER.read_text()
        assert "DO NOT DELETE" in text and "never copied to the public mirror" in text


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Point the helper at an empty folder, so both kinds of copy can be staged."""
    monkeypatch.setattr(conftest, "ROOT", tmp_path)
    monkeypatch.setattr(conftest, "MARKER", tmp_path / "PRIVATE-REPO")
    return tmp_path


def test_in_the_private_repository_a_missing_private_file_fails_and_never_skips(elsewhere):
    """The marker is here and CLAUDE.md is not. A test that asks for it must
    FAIL on the missing file. It must not be skipped: a skip would hide the
    loss of the constitution, a runbook or a workflow."""
    (elsewhere / "PRIVATE-REPO").write_text("x")
    try:
        path = private_file("CLAUDE.md")
    except pytest.skip.Exception:
        # caught on purpose: left alone, a skip raised here would mark THIS test
        # as skipped, and the very thing it guards against would pass unseen
        pytest.fail("a missing private file was skipped in the private repository")
    assert path == elsewhere / "CLAUDE.md"
    with pytest.raises(FileNotFoundError):
        path.read_text()
    assert published_file("README-public.md", "README.md") == elsewhere / "README-public.md"
    (elsewhere / "README.md").write_text("a stray copy")   # even with the mirror's name present
    assert published_file("README-public.md", "README.md") == elsewhere / "README-public.md"


def test_on_a_public_copy_a_missing_private_file_skips_and_names_it(elsewhere):
    with pytest.raises(pytest.skip.Exception, match=r"needs \.github/workflows/ci\.yml, which only "
                                                    r"the private operations repository has"):
        private_file(".github/workflows/ci.yml")
    (elsewhere / "CLAUDE.md").write_text("present after all")
    assert private_file("CLAUDE.md").read_text() == "present after all"


def test_a_file_the_mirror_renames_is_found_under_either_name(elsewhere):
    (elsewhere / "README.md").write_text("public")
    assert published_file("README-public.md", "README.md").read_text() == "public"
    assert published_file("mirror/verify.yml", ".github/workflows/verify.yml") == (
        elsewhere / ".github/workflows/verify.yml"), "missing on a public copy: read fails, no skip"
    (elsewhere / "PRIVATE-REPO").write_text("x")
    (elsewhere / "README-public.md").write_text("private source")
    assert published_file("README-public.md", "README.md").read_text() == "private source"
