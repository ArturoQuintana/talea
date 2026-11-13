"""What the public mirror carries, and how the tests know where they are.

The public README tells a reader to run the suite from the mirror. That is only
true if (a) the mirror's file set is exactly the list in `scripts/mirror_tree.sh`
and nothing private, and (b) every test that needs a file outside that list is
skipped there by name, while in the private repository nothing skips at all.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import conftest
from conftest import private_file, published_file

ROOT = Path(__file__).resolve().parents[1]
TREE = ROOT / "scripts" / "mirror_tree.sh"

# The whole list. A change to what the mirror carries is a PUBLIC change: it
# must change this, where a reader of the pull request will see it.
DATA_INCLUDES = {'--include="/$s/***"', "--include='/esios_prices.json'",
                 "--include='/calibration/***'", "--include='/README-MOVED.md'"}
FOLDERS = ["Data/", "src/", "tests/", "scripts/"]
ROOT_FILES = {"VERIFY.md", "GOVERNANCE.md", "DATA-SOURCES.md", "pyproject.toml", "uv.lock",
              ".python-version", "LICENSE"}
DOCS = {"backtest-baselines-2015-2026.md", "backtest-markets-2026-08.md", "ARCHITECTURE.md",
        "gate-analysis-plan.md", "gate-verdict-2026-08.md", "incidents.md",
        "research-track-records-2026-08.md", "knowledge-map.md"}
RENAMED = {"README-public.md": "README.md", "mirror/verify.yml": ".github/workflows/verify.yml"}
NEVER = ("PRIVATE-REPO", "CLAUDE.md", "INFRA.md", "SECRETS.md", "open-decisions.md",
         "multiple-comparisons-policy.md", "BACKLOG.md", ".env")


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
    "scripts/mirror_tree.sh": "887dd09a705ce81ccf609cdbf7315c97f150afc8753a660eaf173a3f2ca2aef3",
    # (a comment line between the two, so that two pull requests changing one
    # line each can be merged by git)
    "scripts/publish_mirror.sh": "2a7d27b059ff1b3205dc39fca887919268fa56872b371378c21a868fbe6ec108",
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
mkdir -p "$MIRROR" || exit 2
uv run --no-dev python scripts/render_dashboard.py --site "$MIRROR" >&2
mkdir -p "$MIRROR/docs"
INCLUDES=()
for s in $SLUGS; do INCLUDES+=(--include="/$s/***"); done
rsync -a --delete --delete-excluded \
  --exclude='__pycache__' --exclude='.tick.lock' \
  "${INCLUDES[@]}" \
  --include='/esios_prices.json' \
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
    for name in NEVER:
        assert name not in code, f"{name} must never be named in the mirror's list"


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
    # one: a market that is private now, a stray file beside the markets, and
    # files since deleted from the three code folders. Only the copies' own
    # --delete and --delete-excluded remove them.
    stale = ["Data/it/receipts.jsonl", "Data/it/prices.json", "Data/stray.json", "Data/es/gone.jsonl",
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
    assert {p.name for p in (tree / "Data").iterdir()} == set(public) | {
        "esios_prices.json", "calibration", "README-MOVED.md"}
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
    # the folder it is given. rsync, cp and mkdir are stand-ins that only record
    # that they were started, unless the test asks for the real ones.
    lookup_bin, stand_ins = tmp_path / "bin-uv", tmp_path / "bin-writers"
    lookup_bin.mkdir()
    stand_ins.mkdir()
    (lookup_bin / "uv").write_text(f'''#!/bin/sh
echo "uv $*" >> "{started}"
case "$*" in
  *"markets --public"*) [ -n "${{LOOKUP_FAILS:-}}" ] || echo es;;
  *"render_dashboard.py --site"*)
    if [ -n "${{REAL_WRITES:-}}" ]; then for last; do :; done; printf 'the rendered page\\n' > "$last/index.html"; fi
    if [ -n "${{DURING_THE_BUILD:-}}" ]; then sh -c "$DURING_THE_BUILD"; fi;;
esac
''')
    (lookup_bin / "uv").chmod(0o755)
    for name in ("rsync", "cp", "mkdir"):
        (stand_ins / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{started}"\n')
        (stand_ins / name).chmod(0o755)

    def run(folder, cwd=repo, script=repo / "scripts" / "mirror_tree.sh", home_set=True,
            real_writes=False, lookup_fails=False):
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
        r = subprocess.run([shutil.which("bash"), str(script), str(folder)], cwd=cwd, text=True,
                           capture_output=True, timeout=60,   # a guard that never answers is red
                           env=env)
        r.started = started.read_text().splitlines() if started.exists() else []
        r.untouched = _snapshot(home) == before
        return r

    def publish(mirror, during_the_build: str = "", also_set: dict[str, str] | None = None,
                started_as: str | None = None, started_in: Path = home):
        """The real publisher from the made-up checkout, with the real git, rsync,
        cp and mkdir, into `mirror`. Everything it can reach is under tmp_path.
        `during_the_build` is a command the stand-in renderer runs, for the tests
        in which something changes while the tree is built. `started_as` and
        `started_in` are the name it is started by and the folder it is started
        from, when a test needs those to be relative."""
        assert not os.path.isabs(str(mirror)) or str(mirror).startswith(str(tmp_path)), "real writes only under tmp_path"
        if started.exists():
            started.unlink()
        before = _snapshot(home)
        r = subprocess.run([shutil.which("bash"), started_as or str(repo / "scripts" / "publish_mirror.sh")],
                           cwd=started_in, text=True, capture_output=True, timeout=120,
                           env={"PATH": f"{lookup_bin}:{Path(shutil.which('git')).parent}:/usr/bin:/bin",
                                "HOME": str(home), "MIRROR_DIR": str(mirror), "REAL_WRITES": "1",
                                "DURING_THE_BUILD": during_the_build, **(also_set or {}),
                                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        r.started = started.read_text().splitlines() if started.exists() else []
        r.untouched = _snapshot(home) == before
        return r

    run.home, run.repo, run.top, run.publish = home, repo, tmp_path, publish
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


@needs_git_and_rsync
def test_the_publisher_does_nothing_when_git_is_a_link_into_another_repository(staged):
    """The mirror folder's `.git` is a link to the private checkout's own git
    folder. `[ -d "$MIRROR/.git" ]` follows the link and is true. Left alone,
    `git add -A && git commit` in the mirror folder would write a commit into
    the PRIVATE repository. Nothing is built, nothing staged, exit 0."""
    _repository(staged.repo)
    mirror = staged.home / "the-mirror"
    mirror.mkdir()
    (mirror / ".git").symlink_to(staged.repo / ".git")
    head = _git(staged.repo, "rev-parse", "HEAD")
    r = staged.publish(mirror)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/.git is a link, not the mirror's own git folder" in r.stdout, r.stdout + r.stderr
    assert "nothing built or published" in r.stdout and "published [" not in r.stdout
    assert r.started == [], f"the build was started: {r.started}"
    assert _git(staged.repo, "rev-parse", "HEAD") == head and r.untouched


@needs_git_and_rsync
def test_the_publisher_does_nothing_when_gits_working_tree_is_another_folder(staged):
    """The mirror folder holds a real `.git` whose `core.worktree` points at the
    private checkout. To git, the tree to stage is then the private checkout.
    The publisher asks git which folder its working tree is, and it is not the
    mirror folder: nothing is built, nothing staged, nothing pushed, exit 0."""
    mirror, origin = _a_mirror(staged)
    _git(mirror, "config", "core.worktree", str(staged.repo))
    state = _own_state(mirror)
    r = staged.publish(mirror)
    assert _own_state(mirror) == state, "the mirror's repository changed although publication was refused"
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git's working tree for" in r.stdout and "is not that folder" in r.stdout, r.stdout + r.stderr
    assert "nothing built or published" in r.stdout and "published [" not in r.stdout
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
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git did not list GIT_COMMON_DIR among its repository variables" in r.stdout, r.stdout + r.stderr
    assert "published [" not in r.stdout and r.started == [] and r.untouched
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
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git's folder for" in r.stdout and "is not its own .git (it says: '--absolute-git-dir')" in r.stdout, r.stdout + r.stderr
    assert "published [" not in r.stdout and r.started == [] and r.untouched
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
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git's common folder for" in r.stdout and "is not its own .git" in r.stdout, r.stdout + r.stderr
    assert "nothing built or published" in r.stdout and "published [" not in r.stdout
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
    once, with CDPATH cleared, to nothing: nothing is built, and the folder on
    CDPATH and its origin are exactly as they were."""
    elsewhere = staged.home / "found-by-cdpath"
    competing, origin = elsewhere / "the-mirror", elsewhere / "the-mirror-origin.git"
    competing.mkdir(parents=True)
    (competing / "README.md").write_text("some other repository\n")
    _repository(competing, origin)
    state = _own_state(competing)
    r = staged.publish("the-mirror", also_set={"CDPATH": str(elsewhere)})
    assert r.returncode == 0 and "the-mirror not initialized (no such folder); skipping" in r.stdout, r.stdout + r.stderr
    assert r.started == [] and r.untouched and _own_state(competing) == state
    assert _git(origin, "rev-list", "--count", "main").strip() == "1"


@needs_git_and_rsync
def test_the_publisher_asks_again_after_the_build_and_before_staging(staged):
    """The repository is the mirror's own when the publisher starts, and its
    working tree is pointed at the private checkout WHILE the tree is being
    built. The publisher asks git a second time before `git add`: the tree is
    built, and nothing is staged or pushed."""
    mirror, origin = _a_mirror(staged)
    state = _own_state(mirror)
    r = staged.publish(mirror, during_the_build=f"git -C '{mirror}' config core.worktree '{staged.repo}'")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (mirror / "Data/es/receipts.jsonl").exists(), "the tree was not built: this test proved nothing"
    assert "built but NOT published" in r.stdout and "published [" not in r.stdout, r.stdout + r.stderr
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
    script went on to start: the lookup, the render and the copies, every one
    of them aimed at the folder as it is on disk."""
    places = {"repo": staged.repo, "home": staged.home}
    r = staged(folder.format(**places), cwd=places[given_from])
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["es"]
    where = {"{home}/ledger-mirror": "ledger-mirror", "{home}/elsewhere": "elsewhere",
             "{home}/elsewhere/new": "elsewhere/new", "../elsewhere": "elsewhere",
             "link-out": "elsewhere", "{home}/mirror": "mirror"}[folder]
    target = str((staged.home / where).resolve())
    assert r.started[0].startswith("uv ") and "markets --public" in r.started[0], "the lookup comes first"
    assert r.started[1] == f"mkdir -p {target}", "and only then is the folder created"
    copies = [l for l in r.started if l.startswith(("rsync ", "cp "))]
    assert len(copies) >= 9 and all(f" {target}/" in l for l in copies), copies


def test_the_publisher_says_why_nothing_was_built():
    """The publisher never fails the tick, so when the builder refuses or cannot
    resolve the markets it exits 0. Its line must not blame the market lookup
    for a refusal: it gives the builder's exit code and points at its reason."""
    publish = _code((ROOT / "scripts" / "publish_mirror.sh").read_text())
    assert 'SLUGS="$(bash scripts/mirror_tree.sh "$MIRROR")"; BUILT=$?' in publish
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
