"""Regression tests for incident 2026-11-03..09 (independent auditor, 2026-11-09):
the server's private-repo clone DIVERGED from origin — a push was rejected while
the ERCOT/JP GitHub Actions writers pushed to main, and from then on every
pre-tick `git pull --ff-only` failed and every git_backup push was rejected
again. ES/DE/GB stopped reaching origin and ERCOT stopped reaching the server
(hence the public mirror: page "as of 11-08", data stopped 11-03).

These run REAL git against a bare origin with two writers on disjoint
Data/<slug>/ paths — the way the bug actually happened."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Bound here, at collection, and only here: once the whole tree is collected, a
# later `import conftest` can return another folder's conftest.py
# (tools/esios-fetcher/tests has one), which happened in the full suite.
import conftest
import talea.__main__ as cli
from conftest import private_file

ROOT = Path(__file__).resolve().parents[1]

# NOTHING IN THIS FILE IS SKIPPED WHEN GIT IS MISSING. Git is mandatory here:
# the behaviour tests drive real git, and the pin on the workflows reads git's
# index. This file used to carry a module-wide `skipif(git is not installed)`;
# with it, a machine without git reported the pin, and the test that says the
# pin never skips, as skipped. A test that needs git and cannot find it FAILS,
# with the message below. The tests that read only text or a folder (what
# counts as a grant, the lock setting, the server scripts, the cases staged in
# a copy of the folder) need no git and simply run.


def _need_git() -> None:
    assert shutil.which("git") is not None, (
        "git is not installed, or is not on PATH. This test drives real git or reads git's "
        "index; without git it FAILS. It is never skipped: a check that skips when it cannot "
        "look has gone blind")


def _git(repo: Path, *args: str) -> str:
    _need_git()
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def _diverged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """origin (bare) + `server` clone with an UNPUSHED Data/es commit + an
    `actions` clone that has pushed a Data/ercot commit meanwhile."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                   capture_output=True)
    for r in (seed,):
        _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
        _git(r, "checkout", "-q", "-b", "main")
    _write(seed, "Data/es/receipts.jsonl", "es-1\n")
    _write(seed, "Data/ercot/receipts.jsonl", "ercot-1\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "main")
    server, actions = tmp_path / "server", tmp_path / "actions"
    for r in (server, actions):
        subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(r)],
                       check=True, capture_output=True)
        _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    _write(actions, "Data/ercot/receipts.jsonl", "ercot-1\nercot-2\n")
    _git(actions, "commit", "-qam", "data: ercot tick")
    _git(actions, "push", "-q", "origin", "main")
    return origin, server, actions


def test_git_backup_recovers_from_a_rejected_push(tmp_path, monkeypatch, capsys):
    """Server commits Data/es while Actions has pushed Data/ercot: the plain push
    is rejected (non-fast-forward). git_backup must rebase its unpushed commit
    onto origin and push — origin ends with BOTH writers' data, nothing lost,
    nothing force-pushed."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    monkeypatch.setattr(cli, "REPO", server)
    cli.git_backup("2026-11-04")
    out = capsys.readouterr().out
    assert "backup: pushed" in out, out
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(check)],
                   check=True, capture_output=True)
    assert (check / "Data/es/receipts.jsonl").read_text() == "es-1\nes-2\n"
    assert (check / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nercot-2\n"
    # the server now also has the Actions writer's data (mirror source)
    assert (server / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nercot-2\n"


# Every server-side script that syncs the private clone before working.
PULL_SCRIPTS = ("server_tick.sh", "weekly_maintenance.sh")


def _pull_block(script: str) -> str:
    """The pre-run pull command(s) from scripts/<script>, verbatim."""
    text = (ROOT / "scripts" / script).read_text()
    m = re.search(r"^git pull --ff-only.*?(?<!\\)\n", text, re.M | re.S)
    assert m, f"{script} lost its pre-run pull"
    return m.group(0)


@pytest.mark.parametrize("script", PULL_SCRIPTS)
def test_server_pull_recovers_from_diverged_history(tmp_path, script):
    """The bare `git pull --ff-only` failed every tick for six days once the
    clone had diverged (and weekly_maintenance.sh hid the same failure behind
    `|| true`). The pre-run pull must bring the Actions writers' data in AND
    keep the server's unpushed commit (so the run's push succeeds)."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    _git(server, "commit", "-qam", "data: tick (push was rejected)")
    subprocess.run(["bash", "-c", "set -uo pipefail\n" + _pull_block(script)],
                   cwd=server, check=True, capture_output=True, text=True)
    assert (server / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nercot-2\n"
    assert (server / "Data/es/receipts.jsonl").read_text() == "es-1\nes-2\n"
    _git(server, "push", "-q", "origin", "main")          # now a fast-forward
    assert _git(server, "status", "--porcelain") == ""


@pytest.mark.parametrize("script", PULL_SCRIPTS)
def test_server_pull_aborts_cleanly_on_conflict(tmp_path, script):
    """Writers own disjoint paths, but if a conflict ever happens the rebase
    must be ABORTED (never left half-applied) and local data kept intact."""
    origin, server, actions = _diverged(tmp_path)
    _write(server, "Data/ercot/receipts.jsonl", "ercot-1\nlocal-conflict\n")
    _git(server, "commit", "-qam", "conflicting")
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + _pull_block(script)],
                       cwd=server, capture_output=True, text=True)
    assert "pull failed (continuing on local state)" in r.stdout
    assert not (server / ".git" / "rebase-merge").exists()
    assert not (server / ".git" / "rebase-apply").exists()
    assert (server / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nlocal-conflict\n"


def _dirty_overlap(tmp_path: Path) -> Path:
    """Diverged server clone whose working tree ALSO has an uncommitted edit to a
    file the incoming commits changed — the one case where stashing bites."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    _git(server, "commit", "-qam", "data: tick (push was rejected)")
    _write(server, "Data/ercot/receipts.jsonl", "ercot-1\nlocal-uncommitted\n")
    return server


@pytest.mark.parametrize("script", PULL_SCRIPTS)
def test_server_pull_never_writes_conflict_markers_into_a_dirty_file(tmp_path, script):
    """Independent review, 2026-11-10: `git pull --rebase --autostash` on a dirty
    tree stashes, rebases, re-applies — and when the re-apply conflicts it EXITS 0
    with `<<<<<<<` markers left in the file. In an audit trail that file is a
    receipts/ledger/prices file, and the tick's `git add Data` would commit and
    push the markers. The pull must refuse instead (as the old --ff-only did),
    leaving the uncommitted file byte-for-byte as it was."""
    server = _dirty_overlap(tmp_path)
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + _pull_block(script)],
                       cwd=server, capture_output=True, text=True)
    body = (server / "Data/ercot/receipts.jsonl").read_text()
    assert "<<<<<<<" not in body and ">>>>>>>" not in body, body
    assert body == "ercot-1\nlocal-uncommitted\n"
    assert "pull failed (continuing on local state)" in r.stdout
    assert _git(server, "stash", "list") == ""            # nothing left stashed either


def test_weekly_push_never_writes_conflict_markers_into_a_dirty_file(tmp_path):
    """Same hazard in the weekly run's closing loop: it must fail the unit
    loudly rather than 'succeed' with markers in a data file."""
    server = _dirty_overlap(tmp_path)
    r = _run_weekly_push(server)
    body = (server / "Data/ercot/receipts.jsonl").read_text()
    assert "<<<<<<<" not in body and body == "ercot-1\nlocal-uncommitted\n"
    assert r.returncode == 1 and "PUSH-FAILED" in r.stdout
    assert _git(server, "stash", "list") == ""


def test_git_backup_never_autostashes(tmp_path, monkeypatch, capsys):
    """git_backup's self-heal runs right after `git add Data` + commit, so Data is
    clean — but a dirty NON-Data file plus --autostash is the same hazard in a
    code file. It must rebase without stashing; a dirty tree then makes the
    rebase refuse and the failure is logged, not papered over."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    seen = []
    real_run = subprocess.run
    def spy(cmd, **kw):
        seen.append(cmd)
        return real_run(cmd, **kw)
    monkeypatch.setattr(cli, "REPO", server)
    monkeypatch.setattr(cli.subprocess, "run", spy)
    cli.git_backup("2026-11-04")
    assert "backup: pushed" in capsys.readouterr().out
    assert any("pull" in c for c in seen), "the rejected push must trigger the rebase"
    assert not any("--autostash" in c for c in seen)


# ---- weekly_maintenance.sh: the final sync+push (esios-ots-upgrade.service) ----

def _weekly_push_block() -> str:
    """The closing sync+push loop of scripts/weekly_maintenance.sh, verbatim."""
    text = (ROOT / "scripts" / "weekly_maintenance.sh").read_text()
    m = re.search(r"^for attempt in .*?^exit 1\n", text, re.M | re.S)
    assert m, "weekly_maintenance.sh lost its sync+push retry loop"
    return m.group(0)


def _run_weekly_push(server: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", "set -uo pipefail\n" + _weekly_push_block()],
                          cwd=server, capture_output=True, text=True,
                          env={**os.environ, "WEEKLY_PUSH_RETRY_SLEEP": "0"})


def test_weekly_push_recovers_when_origin_moved_during_the_run(tmp_path):
    """2026-11-08: esios-ots-upgrade.service FAILED — the weekly run committed
    its OTS upgrades, then its bare `git push` was rejected (non-fast-forward)
    and the commit stayed on the server. The run holds the checkout ~7 minutes
    inside the ERCOT Actions push window, so origin moving meanwhile is the
    expected case: the closing loop must rebase the weekly commit onto origin
    and push — exit 0, both writers' data on origin, nothing force-pushed."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/es/ots/2026-11-08.txt.ots", "upgraded\n")
    _git(server, "add", "Data")
    _git(server, "commit", "-qm", "data: weekly maintenance")
    r = _run_weekly_push(server)
    assert r.returncode == 0, r.stdout + r.stderr
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(check)],
                   check=True, capture_output=True)
    assert (check / "Data/es/ots/2026-11-08.txt.ots").read_text() == "upgraded\n"
    assert (check / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nercot-2\n"
    assert _git(server, "rev-list", "--count", "origin/main..HEAD") == "0"


def test_weekly_push_with_nothing_new_is_a_clean_success(tmp_path):
    """A quiet week (no Data change, origin unchanged) must still exit 0 — the
    loop runs unconditionally so it can flush an EARLIER unpushed commit."""
    origin, server, _ = _diverged(tmp_path)
    _git(server, "pull", "-q", "--ff-only")               # in sync, nothing to push
    r = _run_weekly_push(server)
    assert r.returncode == 0, r.stdout + r.stderr


def test_weekly_push_fails_loudly_and_cleanly_on_conflict(tmp_path):
    """If the rebase ever conflicts, the unit must FAIL (exit 1, so systemd shows
    it) with the rebase aborted — never half-applied — and the local commit and
    data intact for the next tick's git_backup to retry."""
    origin, server, _ = _diverged(tmp_path)
    _write(server, "Data/ercot/receipts.jsonl", "ercot-1\nlocal-conflict\n")
    _git(server, "commit", "-qam", "conflicting")
    r = _run_weekly_push(server)
    assert r.returncode == 1
    assert "PUSH-FAILED" in r.stdout
    assert not (server / ".git" / "rebase-merge").exists()
    assert not (server / ".git" / "rebase-apply").exists()
    assert (server / "Data/ercot/receipts.jsonl").read_text() == "ercot-1\nlocal-conflict\n"
    assert _git(server, "rev-list", "--count", "origin/main..HEAD") == "1"


# ---- class guard: no private-repo writer script may sync without a fallback ----

# publish_mirror.sh pushes the PUBLIC mirror checkout (~/ledger-mirror), which
# has exactly one writer (the server) and so cannot diverge this way.
SINGLE_WRITER_SCRIPTS = {"publish_mirror.sh"}


def _code_lines(path: Path) -> list[str]:
    return [l for l in path.read_text().splitlines() if not l.lstrip().startswith("#")]


def test_no_server_script_syncs_the_private_repo_without_a_fallback():
    """The class behind incident 2026-11-03..09: three writers share main, so a
    bare `git pull --ff-only` wedges forever once diverged and a bare `git push`
    is rejected whenever another writer got there first. The resolver fixed
    server_tick.sh + git_backup but weekly_maintenance.sh — the third server-side
    writer — still had both. Any script under scripts/ that pulls or pushes the
    private repo must carry the rebase fallback, so a future script cannot
    reintroduce the bare form."""
    seen = set()
    for script in sorted((ROOT / "scripts").glob("*.sh")):
        if script.name in SINGLE_WRITER_SCRIPTS:
            continue
        code = "\n".join(_code_lines(script))
        if "git pull" not in code and "git push" not in code:
            continue
        seen.add(script.name)
        assert "git pull --ff-only || true" not in code, \
            f"{script.name}: a swallowed --ff-only pull hides a diverged clone"
        if "git pull --ff-only" in code:
            assert "git pull --rebase" in _pull_block(script.name), \
                f"{script.name}: --ff-only pull has no rebase fallback"
        assert "--autostash" not in code, (
            f"{script.name}: --autostash can exit 0 with conflict markers left in a "
            f"dirty data file — rebase without it and let a dirty tree refuse")
        for line in code.splitlines():
            if re.search(r"\bgit push\b", line):
                assert "git pull --rebase" in line, (
                    f"{script.name}: bare `git push` (no rebase-retry): {line.strip()}")
    assert seen == set(PULL_SCRIPTS), (
        f"private-repo sync scripts changed: {sorted(seen)} — add the new one to "
        f"PULL_SCRIPTS so the real-git recovery tests cover it")


# ---- the Actions writers (ercot.yml, jp.yml): their commit-and-push step ----

WORKFLOWS = ROOT / ".github" / "workflows"

# EVERY file in .github/workflows/, by name: the sha256 of its bytes, and, if it
# is a WRITER (a workflow whose own token may write to the repository), the
# market it writes.
#
# Every workflow is a reserved file. This table does not read them, so there is
# no spelling, step, key, environment variable or quoting that gets a change
# past it: one changed byte, a new file, a removed file, a renamed file, and
# the test below fails and names the file. IF THE CHANGE IS INTENDED: update
# that file's line here, in the same pull request, and say in the pull request
# what changed and why.
#
# What this is NOT: a judgement of the change. A pull request that changes a
# workflow changes its line here too; the table only makes the change
# impossible to miss. Judging it is the readers' and the operator's work.
#
# TWO THINGS ARE PINNED, AND BOTH MUST HOLD: what the folder shows (every entry
# of .github/workflows/ a regular file with the pinned bytes), and WHAT GIT
# RECORDS: at each workflow path a regular file (mode 100644) holding exactly
# those bytes, and nothing else recorded under .github. The second is read
# from the index of the checkout the suite runs in; in CI that is the merge
# commit GitHub checked out. A folder can look right while git records
# something else (a link, another repository's commit in place of the folder,
# other bytes behind a filter or an unchanged-flag), and GitHub runs what git
# records. WHERE THERE IS NO GIT REPOSITORY THE SECOND CANNOT BE READ, AND
# THAT IS A FAILURE, NEVER A SKIP: in the private repository the suite always
# runs in a checkout, and a check that skips when it cannot look is a check
# gone blind. The one place these tests do not apply is a PUBLIC COPY of
# these tests (the mirror), which carries none of these workflows: there,
# every test that reads them is skipped by name through `_private_workflows()`
# (tests/conftest.py's `private_file`), which skips ONLY where the marker file
# that only the private repository has is absent and the workflow is absent
# too. In the private repository it never skips: a missing workflow is read,
# and fails. A missing git repository or a missing git program is never a
# reason to skip, anywhere.
#
# And it covers THE WORKFLOW'S OWN FILE ONLY. "Nothing gets a change past it"
# is true of that file's bytes and of nothing else. What a writer does also
# depends on things that never touch this table. This list is not exhaustive:
#   - a change to these comes before the readers by its own diff: the tick's
#     code, pyproject.toml, uv.lock, the Python pin;
#   - a change to these comes before nobody: the tags of the actions it uses,
#     the runner image, uv's version, the repository's default permission for
#     a workflow's token, whether the workflow is enabled in Actions, the rules
#     on main, the secrets and the organisation's settings, the input data and
#     the outside services the tick calls.
#
# (A comment line stands between every two entries on purpose: git merges two
# pull requests that change two different lines only if an unchanged line lies
# between them.)
PINNED = {
    # the one job `test`: the append-only check, the suite, the ledger re-derivation
    "ci.yml": {"sha256": "2750fdd1928adfc82bbe1de672148b7a52035787da6a17b7a9599d8ede02ff1d", "writes": None},
    # WRITER: ERCOT's ladder; commits and pushes Data/ercot
    "ercot.yml": {"sha256": "206109fc44cb5115cceb79f5ce9024aaf3871c67d855444ccdc315bc36066ac5", "writes": "ercot"},
    # WRITER: JP's ladder; commits and pushes Data/jp
    "jp.yml": {"sha256": "e57fa21cde53fcb2c7e50317ebe7a47dc409e2fba38553ad83b3203cfe30bb7f", "writes": "jp"},
    # read-only: every registered market still reaches origin
    "liveness.yml": {"sha256": "a479901cc898052651cc5ff522a9fcb8c16bdd464cd131fdf05da7cf5cc109af", "writes": None},
    # read-only on the repository: the red-main alert (issues and email)
    "red-main.yml": {"sha256": "ffcd095bc2e7fca537478fdbf0b54c56459d12696bec8fbe81bf0978ed6a81c9", "writes": None},
}
# workflow file -> the market it writes. The behaviour tests below run for
# exactly these; that a file is a writer is a fact written in the table by a
# person, not a guess from the words in the file.
WRITER_WORKFLOWS = {name: e["writes"] for name, e in PINNED.items() if e["writes"]}


def _private_workflows() -> list[Path]:
    """Every workflow in the table, as a path. On a PUBLIC copy of these tests,
    where the workflows are absent, the calling test is skipped and the reason
    names the first missing one. In the private repository the paths come back
    whether or not the files exist, so a missing workflow fails where it is read
    (see tests/conftest.py)."""
    return [private_file(f".github/workflows/{name}") for name in sorted(PINNED)]


def test_in_the_private_repository_the_workflow_tests_never_skip():
    """`_private_workflows()` skips only on a public copy. Here, where the
    marker is, it must hand back the paths and skip nothing. Caught on purpose:
    left alone, a skip raised here would mark THIS test as skipped, and every
    test that calls the helper would go blind with it, unseen."""
    if not conftest.is_private_repo():
        private_file(".github/workflows/ci.yml")    # a public copy: skipped there by name, like the rest
    # (A deleted marker in the private repository is caught by name elsewhere:
    # tests/test_mirror_tree.py requires the marker and CLAUDE.md together.)
    try:
        paths = _private_workflows()
    except pytest.skip.Exception:
        pytest.fail("in the private repository the workflow tests were skipped")
    assert [p.name for p in paths] == sorted(PINNED)


def _commit_step(workflow: str) -> str:
    """The `run: |` script of the workflow's "Commit & push" step, verbatim."""
    _private_workflows()
    lines = (WORKFLOWS / workflow).read_text().splitlines()
    at = [i for i, l in enumerate(lines) if l.strip().startswith("- name: Commit & push")]
    assert len(at) == 1, f"{workflow}: expected exactly one 'Commit & push' step"
    run = lines[at[0] + 1]
    assert run.strip() == "run: |", f"{workflow}: the commit step lost its script"
    indent = len(run) - len(run.lstrip())
    body = []
    for l in lines[at[0] + 2:]:
        if l.strip() and len(l) - len(l.lstrip()) <= indent:
            break
        body.append(l)
    return textwrap.dedent("\n".join(body)) + "\n"


# What a tick leaves in a market's folder, as bytes. A FULL pass: two appended
# files, one rewritten file, and two files that did not exist before and are
# therefore untracked: the day's manifest and its OpenTimestamps PROOF (binary).
PROOF = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94\x01day-2\xff\n"
FULL_PASS = {
    "Data/{slug}/receipts.jsonl": b"{slug}-1\n{slug}-2\n",
    "Data/{slug}/ledger.jsonl": b"settled-1\nsettled-2\n",
    "Data/{slug}/prices.json": b"prices-2\n",
    "Data/{slug}/ots/day-2.txt": b"manifest-2\n",
    "Data/{slug}/ots/day-2.txt.ots": PROOF,
}
# A pass that commits no receipt (it began after the deadline, or the day's
# receipt already stands) but settles an earlier day and stamps.
SETTLES_ONLY = {
    "Data/{slug}/ledger.jsonl": b"settled-1\nsettled-2\n",
    "Data/{slug}/ots/day-2.txt": b"manifest-2\n",
    "Data/{slug}/ots/day-2.txt.ots": PROOF,
}
# A pass with nothing to commit and nothing to settle: it only stamps. Nothing
# TRACKED changes; everything it wrote is new files.
STAMPS_ONLY = {
    "Data/{slug}/ots/day-2.txt": b"manifest-2\n",
    "Data/{slug}/ots/day-2.txt.ots": PROOF,
}


def _named(output: dict, slug: str) -> dict[str, bytes]:
    return {rel.format(slug=slug): data.replace(b"{slug}", slug.encode())
            for rel, data in output.items()}


def _runner(tmp_path: Path, slug: str, *, server_changes_lock: bool,
            server_changes_market: bool = False, output: dict = FULL_PASS) -> tuple[Path, Path]:
    """origin (bare) + a `runner` clone as a workflow run has it after the tick:
    a fresh checkout with everything the tick leaves UNCOMMITTED in
    Data/<slug>/ (see FULL_PASS), while the server has pushed a Data/es commit
    since the checkout (the push race the loop exists for). Optionally that
    server commit also changed uv.lock, as a merged dependency change would, or
    appended its own line to this market's receipts, which no writer should and
    which conflicts. origin also holds a second branch, `archive`, that no
    writer has any business touching."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                   capture_output=True)
    _git(seed, "config", "user.email", "t@t"); _git(seed, "config", "user.name", "t")
    _git(seed, "checkout", "-q", "-b", "main")
    _write(seed, "Data/es/receipts.jsonl", "es-1\n")
    _write(seed, f"Data/{slug}/receipts.jsonl", f"{slug}-1\n")
    _write(seed, f"Data/{slug}/ledger.jsonl", "settled-1\n")
    _write(seed, f"Data/{slug}/prices.json", "prices-1\n")
    _write(seed, f"Data/{slug}/ots/day-1.txt", "manifest-1\n")
    _write(seed, "uv.lock", "lock-1\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "push", "-q", "origin", "main:refs/heads/archive")
    runner = tmp_path / "runner"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(runner)],
                   check=True, capture_output=True)
    _write(seed, "Data/es/receipts.jsonl", "es-1\nes-2\n")
    if server_changes_lock:
        _write(seed, "uv.lock", "lock-2\n")
    if server_changes_market:
        _write(seed, f"Data/{slug}/receipts.jsonl", f"{slug}-1\nnot-this-run\n")
    _git(seed, "commit", "-qam", "data: tick")
    _git(seed, "push", "-q", "origin", "main")
    for rel, data in _named(output, slug).items():
        (runner / rel).parent.mkdir(parents=True, exist_ok=True)
        (runner / rel).write_bytes(data)
    return origin, runner


def _run_commit_step(workflow: str, runner: Path) -> subprocess.CompletedProcess:
    """Run the step as Actions does (`bash -e`), with its retry sleep made
    instant; the script itself is not altered."""
    return subprocess.run(["bash", "-e", "-c", "sleep() { :; }\n" + _commit_step(workflow)],
                          cwd=runner, capture_output=True, text=True)


def _on_origin(tmp_path: Path, origin: Path, rel: str, *, binary: bool = False):
    check = tmp_path / f"check{len(list(tmp_path.glob('check*')))}"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(check)],
                   check=True, capture_output=True)
    return (check / rel).read_bytes() if binary else (check / rel).read_text()


def _delivered(tmp_path: Path, origin: Path, slug: str,
               r: subprocess.CompletedProcess, runner: Path | None = None,
               output: dict = FULL_PASS) -> bool:
    """Whether this run's output is on origin, byte for byte. Asserts what every
    case must hold:

      the step exits 0 IF AND ONLY IF everything the tick wrote is on origin.

    For a pass that wrote a receipt that means the receipt and everything
    written with it (the ledger line, the prices, the manifest and its proof).
    For a pass that wrote no receipt it means whatever it did write: a ledger
    line and a proof, or a proof alone. And nothing in between: if the step
    fails, NONE of the run's output may be on origin.

    After a success the runner's tree must be clean (nothing left behind), and
    origin's other branch must be exactly where it was."""
    check = tmp_path / f"check{len(list(tmp_path.glob('check*')))}"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(check)],
                   check=True, capture_output=True)
    expected = _named(output, slug)
    on_origin = {rel: (check / rel).read_bytes() if (check / rel).exists() else None
                 for rel in expected}
    there = on_origin == expected
    assert (r.returncode == 0) == there, (
        f"exit {r.returncode}, but of what the tick wrote this is on origin: "
        f"{sorted(rel for rel in expected if on_origin[rel] == expected[rel])} "
        f"and this is not: {sorted(rel for rel in expected if on_origin[rel] != expected[rel])}\n"
        + r.stdout + r.stderr)
    if there:
        if runner is not None:
            left = _git(runner, "status", "--porcelain")
            assert left == "", f"the step succeeded and left this behind on the runner:\n{left}"
    else:
        assert all(on_origin[rel] != expected[rel] for rel in expected), \
            "nothing of a failed run's output may be on origin"
    seed = tmp_path / "seed"
    assert _git(origin, "rev-parse", "refs/heads/archive") == _git(
        seed, "rev-list", "--max-parents=0", "HEAD"), \
        "origin's other branch was moved or deleted by the writer's push"
    return there


@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_pushes_its_receipt_when_origin_moved(tmp_path, workflow):
    """What the loop is for: the server pushed between this run's checkout and
    its push. On a clean tree the step must rebase its one commit onto origin
    and push it: exit 0, both writers' lines on origin."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=True)
    r = _run_commit_step(workflow, runner)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _delivered(tmp_path, origin, slug, r, runner)
    assert _on_origin(tmp_path, origin, f"Data/{slug}/ots/day-2.txt.ots", binary=True) == PROOF, \
        "the proof file, byte for byte"
    assert (runner / "Data/es/receipts.jsonl").read_text() == "es-1\nes-2\n"


@pytest.mark.parametrize("output, what", [(SETTLES_ONLY, "a ledger line and a proof"),
                                          (STAMPS_ONLY, "a proof and nothing else")])
@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_pushes_a_pass_that_wrote_no_receipt(tmp_path, workflow, output, what):
    """Found in review, 2026-11-13: every fixture contained a new receipt, so the
    step's early exit ("no changes") could be narrowed to the receipts file, or
    to tracked files only, and nothing noticed. A pass that only settles an
    earlier day, or only stamps, would then end green with its ledger line or
    its proof left on the runner. Whatever the tick wrote must be pushed: here
    {what}."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=False, output=output)
    r = _run_commit_step(workflow, runner)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"no {slug.upper()} changes" not in r.stdout, f"the tick wrote {what}"
    assert _delivered(tmp_path, origin, slug, r, runner, output)
    assert _on_origin(tmp_path, origin, f"Data/{slug}/receipts.jsonl") == f"{slug}-1\n", \
        "and the receipts file is as it was"


@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_with_nothing_written_exits_zero_and_pushes_nothing(tmp_path, workflow):
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=False, output={})
    before = _git(origin, "rev-parse", "refs/heads/main")
    r = _run_commit_step(workflow, runner)
    assert r.returncode == 0 and f"no {slug.upper()} changes" in r.stdout
    assert _git(origin, "rev-parse", "refs/heads/main") == before
    assert _delivered(tmp_path, origin, slug, r, runner, output={}), \
        "nothing written, nothing owed: and the runner clean and origin's other branch untouched"


@pytest.mark.parametrize("server_changes_lock", [True, False])
@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_refuses_a_dirty_tree_and_says_so(tmp_path, workflow,
                                                         server_changes_lock):
    """The two workflows pulled with `--rebase --autostash`. With a tracked file
    modified outside Data/<slug>/ (here uv.lock, which a plain `uv sync` rewrites
    when it is stale) that stashes, rebases and re-applies. When the incoming
    commits changed the same file the re-apply conflicts, and the step still
    EXITS 0 with `<<<<<<<` markers in the file and the edit left in the stash;
    when they did not, the step succeeds as if the tree had been clean. Either
    way a run reports success over a tree it never expected.

    Without --autostash the pull refuses, every attempt. The step must fail
    (exit 1), name the dirty file in its log, leave that file byte for byte as
    it was and nothing stashed. The cost is stated, not hidden: this run's
    receipt is committed on the runner only and does not reach origin."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=server_changes_lock)
    _write(runner, "uv.lock", "lock-local\n")
    r = _run_commit_step(workflow, runner)
    body = (runner / "uv.lock").read_text()
    assert "<<<<<<<" not in body and ">>>>>>>" not in body, body
    assert body == "lock-local\n"
    assert _git(runner, "stash", "list") == ""
    assert r.returncode == 1, r.stdout + r.stderr
    assert "push failed after retries" in r.stdout
    assert "exists only on this runner and is lost with it" in r.stdout
    assert "rides next run" not in r.stdout, "a fresh runner carries nothing forward"
    assert " M uv.lock" in r.stdout, "the log must name what was dirty"
    assert _git(runner, "rev-list", "--count", "origin/main..HEAD") == "1"
    assert not _delivered(tmp_path, origin, slug, r)
    assert _on_origin(tmp_path, origin, f"Data/{slug}/receipts.jsonl") == f"{slug}-1\n"


@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_fails_when_its_commit_conflicts_with_origin(tmp_path, workflow):
    """Found in review, 2026-11-13: with the pull written as
    `(git pull --rebase origin main || true) && git push ...` every other test
    here still passed. If origin's new commit touches this market's receipts the
    rebase stops on a conflict, HEAD is left on origin's commit, and that
    mutant then pushes nothing new and exits 0: a green run, no receipt. The
    step must exit 1, name the conflicted file, and origin must be untouched."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=False,
                             server_changes_market=True)
    r = _run_commit_step(workflow, runner)
    assert r.returncode == 1, r.stdout + r.stderr
    assert not _delivered(tmp_path, origin, slug, r)
    assert "push failed after retries" in r.stdout
    assert f"Data/{slug}/receipts.jsonl" in r.stdout.split("retry 5", 1)[1], \
        "the log must name the conflicted file"
    assert _on_origin(tmp_path, origin, f"Data/{slug}/receipts.jsonl") == f"{slug}-1\nnot-this-run\n"


def _hook(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)


@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_never_overwrites_a_commit_the_other_writer_pushed_in_the_gap(tmp_path, workflow):
    """The race itself: the server pushes AFTER this run's rebase and BEFORE its
    push. (A post-rewrite hook on the runner, which git runs when the rebase has
    finished, makes the server push in that gap, once.) The push that follows
    opens its connection after origin has moved, so:

      - the plain push is rejected, the next attempt pulls the server's commit
        in and pushes: exit 0, both writers' lines on origin;
      - a push that OVERRIDES the rejection of a non-fast-forward would be
        accepted and would erase the server's commit: `--force`, `-f`, a `+`
        refspec on the command line, a mirror push.

    So this test fails for each of those. Two things called forcing do not fail
    it, and are safe here: `--force-with-lease` is rejected in this race (its
    lease is the tracking ref, which predates the commit pushed in the gap) and
    the retry recovers; a `+` refspec set in the configuration has no effect
    while the command itself names `HEAD:main`.
    (An earlier version staged the race from a pre-push hook, which runs after
    the remote's refs are read; there even a forced push is rejected, and
    `git push --force` passed every behaviour test.) origin's second branch
    must be untouched too: a mirror push would delete it."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=False)
    seed, mark = tmp_path / "seed", tmp_path / "raced"
    _hook(runner / ".git" / "hooks" / "post-rewrite", f"""
[ -e "{mark}" ] && exit 0
touch "{mark}"
unset $(git rev-parse --local-env-vars)
printf 'es-1\\nes-2\\nes-3\\n' > "{seed}/Data/es/receipts.jsonl"
git -C "{seed}" commit -qam "data: tick (in the gap)" && git -C "{seed}" push -q origin main
""")
    r = _run_commit_step(workflow, runner)
    assert mark.exists(), "the race was never staged; this test proved nothing"
    assert _on_origin(tmp_path, origin, "Data/es/receipts.jsonl") == "es-1\nes-2\nes-3\n", (
        "the commit the server pushed in the gap is gone from origin: this "
        "run's push overwrote it\n" + r.stdout + r.stderr)
    assert "push race, retry 1" in r.stdout and "retry 2" not in r.stdout, r.stdout + r.stderr
    assert "rejected" in r.stderr or "failed to push" in r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    assert _delivered(tmp_path, origin, slug, r, runner)


@pytest.mark.parametrize("workflow", sorted(WRITER_WORKFLOWS))
def test_actions_writer_fails_when_every_push_is_refused(tmp_path, workflow):
    """Origin refuses each push (as a ruleset or an outage would). The pull
    works every time, so nothing but the push's own exit code can fail the
    step: five attempts, exit 1, the receipt not on origin and said to be lost."""
    slug = WRITER_WORKFLOWS[workflow]
    origin, runner = _runner(tmp_path, slug, server_changes_lock=False)
    _hook(origin / "hooks" / "pre-receive", "echo 'refused by origin' >&2\nexit 1\n")
    r = _run_commit_step(workflow, runner)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.count("push race, retry") == 5
    assert "refused by origin" in r.stderr
    assert "publication was not confirmed" in r.stdout
    assert "exists only on this runner and is lost with it" in r.stdout
    assert not _delivered(tmp_path, origin, slug, r)


def _workflow_files(root: Path = ROOT) -> list[Path]:
    """Every entry of .github/workflows/, and ONE RULE for all of them: each is
    a regular file, judged WITHOUT following links, and neither `.github` nor
    `.github/workflows` is itself a link. Anything else fails here, by name: a
    link (to a file or to nothing), a folder, anything that is not a regular
    file.

    Why: a link is followed by everything that reads a file. A workflow replaced
    by a link to a copy of its own bytes would keep its name, its hash and its
    behaviour tests, while git holds a link at the workflow's path and GitHub no
    longer finds the workflow there. And a sub-folder would hold files this
    table never sees."""
    for folder in (root / ".github", root / ".github" / "workflows"):
        assert stat.S_ISDIR(os.lstat(folder).st_mode), (
            f"{folder.relative_to(root)} is not a real folder (a link?). The workflows "
            f"are pinned as files in the repository, never through a link")
    folder = root / ".github" / "workflows"
    kinds = {name: os.lstat(folder / name).st_mode for name in sorted(os.listdir(folder))}
    odd = [f"{name} ({'a link' if stat.S_ISLNK(mode) else 'a folder' if stat.S_ISDIR(mode) else 'not a regular file'})"
           for name, mode in kinds.items() if not stat.S_ISREG(mode)]
    assert not odd, (
        f".github/workflows/ holds something that is not a regular file: {', '.join(odd)}. "
        f"Every entry must be a workflow file itself: no link, no sub-folder")
    return [folder / name for name in kinds]


def _on_disk(root: Path = ROOT) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in _workflow_files(root)}


def test_every_workflow_file_is_exactly_as_pinned():
    """The whole text guard for the workflows. Three earlier versions tried to
    say in words what a writer workflow may contain (a list of forbidden
    pushes; then the commit step word for word with git allowed nowhere else),
    and each time a reader walked around it: an action that pushes, a step that
    deletes the proofs before the commit step, `if: false`, `continue-on-error`,
    a working directory, an environment variable that redefines git. Reading
    the text loses to the next thing nobody thought of. So nothing is read:
    every file in the folder is pinned by its bytes."""
    _private_workflows()
    _assert_folder_and_git_as_pinned(ROOT)


def _assert_folder_and_git_as_pinned(root: Path) -> None:
    """Both halves, in this order: what the folder shows, then what git records."""
    _assert_as_pinned(_on_disk(root))
    _assert_recorded_as_pinned(root)


def _git_plain(root: Path, *args: str, own_settings_only: bool = False) -> str:
    """git in `root`, with nothing inherited that could point it at another
    repository or index (a hook's GIT_DIR, GIT_INDEX_FILE and the like). For a
    repository a test builds, `own_settings_only` also shuts out this machine's
    and this user's git settings, so that a filter or line-ending rule set
    there cannot change what the test stages."""
    _need_git()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if own_settings_only:
        env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr.strip()}"
    return r.stdout


def _git_made_up(root: Path, *args: str) -> str:
    """git in a repository a test builds. EVERY call that builds or changes
    one goes through here: this machine's and this user's git settings (a
    filter, a line-ending rule, an attributes file) are shut out, so that what
    a test stages is what the test wrote."""
    return _git_plain(root, *args, own_settings_only=True)


_KINDS = {"100644": "a regular file", "100755": "an executable file", "120000": "a link",
          "160000": "a commit of another repository in place of a folder (a gitlink)"}


def _recorded(root: Path) -> dict[str, tuple[str, str]]:
    """What git records under .github in the index of the repository at
    `root`: path -> (mode, blob id). Not what the folder shows."""
    entries: dict[str, tuple[str, str]] = {}
    for item in _git_plain(root, "ls-files", "-s", "-z", "--", ".github").split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            mode, blob, stage = meta.split()
            assert stage == "0", f"{path} is in the middle of a merge (stage {stage})"
            entries[path] = (mode, blob)
    return entries


def _assert_recorded_as_pinned(root: Path) -> None:
    """ONE RULE for what git records: at each workflow path of the table a
    regular file (mode 100644) holding exactly the bytes of the working file
    whose sha256 the table pins, and nothing else recorded under .github.

    The mode catches a link (120000) and a gitlink (160000) whatever the folder
    looks like: `.github` recorded as another repository's commit shows the
    same five files in an initialised checkout while this repository holds no
    workflow at all. The blob comparison (`git hash-object --no-filters` of the
    working file) catches recorded bytes that differ from the file's: a clean
    or smudge filter, a line-ending conversion, an assume-unchanged or
    skip-worktree flag."""
    _need_git()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, env=env)
    assert top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve(), (
        f"{root} is not the top of a git repository ({top.stderr.strip() or top.stdout.strip()}), so "
        f"what git records under .github cannot be read. That is a FAILURE, not a skip: the "
        f"suite of the private repository always runs in a checkout, and a check that skips "
        f"when it cannot look has gone blind")
    recorded = _recorded(root)
    expected = {f".github/workflows/{name}" for name in PINNED}
    named = lambda paths: [f"{q} ({_KINDS.get(recorded[q][0], 'mode ' + recorded[q][0])})" for q in sorted(paths)]
    extra, missing = set(recorded) - expected, expected - set(recorded)
    assert not extra and not missing, (
        f"what git records under .github is not the table's five workflows. Recorded and not "
        f"in the table: {named(extra)}. In the table and not recorded: {sorted(missing)}. "
        f"Every workflow is a reserved file, and nothing else may be recorded under .github "
        f"without changing this rule and the table")
    wrong_kind = [q for q in sorted(recorded) if recorded[q][0] != "100644"]
    assert not wrong_kind, (
        f"git does not record a regular file at: {named(wrong_kind)}. GitHub runs what git "
        f"records, not what the folder shows")
    differs = [q for q in sorted(recorded)
               if recorded[q][1] != _git_plain(root, "hash-object", "--no-filters", "--", q).strip()]
    assert not differs, (
        f"git records other bytes than the working file holds at: {differs}. The table pins "
        f"the file's bytes; what is committed must be those bytes (a filter, a line-ending "
        f"conversion or an unchanged-flag stands between them)")


def _assert_as_pinned(here: dict[str, str]) -> None:
    added, gone = sorted(set(here) - set(PINNED)), sorted(set(PINNED) - set(here))
    assert not added and not gone, (
        f".github/workflows/ no longer holds the files in the table. New: {added}. "
        f"Missing: {gone}. Every workflow is a reserved file: if this is intended, "
        f"add or remove its line in PINNED in this same pull request (saying whether "
        f"it is a writer), and say in the pull request what changed and why")
    changed = sorted(name for name, digest in here.items() if digest != PINNED[name]["sha256"])
    assert not changed, (
        f"changed: {changed}. " + " ".join(
            f"{name} is now sha256 {here[name]}." for name in changed)
        + " Every workflow is a reserved file: if the change is intended, update its "
          "line in PINNED in this same pull request, and say in the pull request what "
          "changed and why")


@pytest.fixture
def copied(tmp_path, tmp_path_factory, monkeypatch) -> Path:
    """A copy of this repository's .github folder, links kept as links, for the
    cases that must not be staged in the real one.

    Every test that uses it ALSO runs with a made-up home whose git settings
    are hostile: a clean filter applied to every .yml through a global
    attributes file, and line-ending conversion. A git that reads those
    settings stages other bytes than the files hold. So a repository a test
    builds from this copy only behaves as the test says if every call that
    builds or changes it shuts this machine's settings out (`_git_made_up`)."""
    _private_workflows()
    shutil.copytree(ROOT / ".github", tmp_path / ".github", symlinks=True)
    hostile = tmp_path_factory.mktemp("home-with-hostile-git-settings")
    (hostile / "attributes").write_text("*.yml filter=mangle\n")
    (hostile / ".gitconfig").write_text(
        f'[core]\n\tattributesFile = {hostile}/attributes\n\tautocrlf = true\n'
        '[filter "mangle"]\n\tclean = tr a-z A-Z\n')
    monkeypatch.setenv("HOME", str(hostile))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def test_a_copy_of_the_folder_reads_exactly_as_the_folder_does(copied):
    """So that the four cases below fail for what was done to the copy and for
    nothing else."""
    assert _on_disk(copied) == _on_disk()
    _assert_as_pinned(_on_disk(copied))


@pytest.mark.parametrize("name", sorted(PINNED))
def test_one_byte_changed_in_a_workflow_fails_and_names_it(copied, name):
    workflow = copied / ".github" / "workflows" / name
    workflow.write_bytes(workflow.read_bytes() + b"\n")
    with pytest.raises(AssertionError, match=rf"changed: \['{re.escape(name)}'\]"):
        _assert_as_pinned(_on_disk(copied))


def test_a_file_added_to_or_removed_from_the_folder_fails_and_names_it(copied):
    folder = copied / ".github" / "workflows"
    (folder / "sixth.yml").write_text("on: push\n")
    with pytest.raises(AssertionError, match=r"New: \['sixth\.yml'\]\. Missing: \[\]"):
        _assert_as_pinned(_on_disk(copied))
    (folder / "sixth.yml").unlink()
    (folder / "liveness.yml").unlink()
    with pytest.raises(AssertionError, match=r"New: \[\]\. Missing: \['liveness\.yml'\]"):
        _assert_as_pinned(_on_disk(copied))


def test_a_workflow_replaced_by_a_link_to_its_own_bytes_fails(copied):
    """Same name, same bytes behind the link, so the same hash: only the rule
    that an entry must be a regular file can see it."""
    workflow = copied / ".github" / "workflows" / "ercot.yml"
    body = copied / ".github" / "ercot-body.yml"
    workflow.rename(body)
    workflow.symlink_to("../ercot-body.yml")
    assert hashlib.sha256(workflow.read_bytes()).hexdigest() == PINNED["ercot.yml"]["sha256"], (
        "the staging is wrong: through the link the bytes must be the pinned ones")
    with pytest.raises(AssertionError, match=r"ercot\.yml \(a link\)"):
        _on_disk(copied)


@pytest.mark.parametrize("linked", [".github/workflows", ".github"])
def test_the_folder_itself_being_a_link_fails(copied, linked):
    """Every file reads the same through the link; the folder is not in the
    repository as a folder any more."""
    real = copied / linked
    moved = copied / "moved-away"
    real.rename(moved)
    real.symlink_to(moved, target_is_directory=True)
    assert sorted(p.name for p in (copied / ".github" / "workflows").iterdir()) == sorted(PINNED), (
        "the staging is wrong: through the link the same five files must be seen")
    with pytest.raises(AssertionError, match=re.escape(linked) + r" is not a real folder"):
        _on_disk(copied)


def test_a_dangling_link_added_to_the_folder_fails(copied):
    """`is_file()` is false for a link to nothing, so a filter on it would never
    see this entry at all."""
    (copied / ".github" / "workflows" / "extra.yml").symlink_to("nowhere.yml")
    with pytest.raises(AssertionError, match=r"extra\.yml \(a link\)"):
        _on_disk(copied)


def test_a_sub_folder_holding_a_workflow_fails(copied):
    sub = copied / ".github" / "workflows" / "sub"
    sub.mkdir()
    shutil.copy(copied / ".github" / "workflows" / "ercot.yml", sub / "extra.yml")
    with pytest.raises(AssertionError, match=r"sub \(a folder\)"):
        _on_disk(copied)


@pytest.fixture
def recorded_copy(copied) -> Path:
    """The copy of .github made into a repository of its own, the five
    workflows added to its index and nothing committed."""
    _git_made_up(copied, "init", "-q")
    _git_made_up(copied, "add", "--", ".github")
    return copied


def test_what_git_records_in_a_plain_repository_is_as_pinned(recorded_copy):
    assert sorted(_recorded(recorded_copy)) == sorted(f".github/workflows/{n}" for n in PINNED)
    assert {mode for mode, _ in _recorded(recorded_copy).values()} == {"100644"}
    _assert_recorded_as_pinned(recorded_copy)


def test_this_machines_git_settings_cannot_change_what_a_test_stages(copied, tmp_path):
    """The made-up home that `copied` sets has hostile git settings. A git that
    reads them stages other bytes than the files hold: shown first, so that the
    settings are known to bite on this machine's git. A repository built the
    way every test here builds one does not read them, and stages the files'
    own bytes."""
    exposed = tmp_path / "exposed"
    shutil.copytree(copied / ".github", exposed / ".github")
    _git_plain(exposed, "init", "-q")                        # reads the hostile settings
    _git_plain(exposed, "add", "--", ".github")
    with pytest.raises(AssertionError, match="records other bytes than the working file holds"):
        _assert_recorded_as_pinned(exposed)
    made_up = tmp_path / "made-up"
    shutil.copytree(copied / ".github", made_up / ".github")
    _git_made_up(made_up, "init", "-q")
    _git_made_up(made_up, "add", "--", ".github")
    _assert_recorded_as_pinned(made_up)


def test_without_the_git_program_the_pin_fails_and_nothing_is_skipped(tmp_path):
    """pytest itself, started on this file with a PATH that holds no git. The
    table test must be reported FAILED (not skipped, as the module-wide skip
    this file used to carry reported it), with the message that says why; and a
    test that needs no git must be reported passed, so that nothing in the file
    is skipped for the lack of git."""
    _private_workflows()
    nothing = tmp_path / "a-path-with-nothing-on-it"
    nothing.mkdir()
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA",
         f"{Path(__file__).name}::test_every_workflow_file_is_exactly_as_pinned",
         f"{Path(__file__).name}::test_what_counts_as_a_grant_of_write_access"],
        cwd=Path(__file__).parent, capture_output=True, text=True, timeout=300,
        env={"PATH": str(nothing), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    out = r.stdout + r.stderr
    assert "FAILED test_git_divergence.py::test_every_workflow_file_is_exactly_as_pinned" in out, out[-3000:]
    assert "git is not installed, or is not on PATH" in out and "It is never skipped" in out, out[-3000:]
    assert re.search(r"\b1 failed, \d+ passed\b", out), out[-1500:]
    assert "skipped" not in out.splitlines()[-1] and "SKIPPED" not in out, out[-1500:]
    assert r.returncode == 1, (r.returncode, out[-1500:])


def test_the_table_test_reads_what_git_records_here(monkeypatch):
    """The cases below call the rule on repositories built for them. This one
    is about the real test on the real repository: if git recorded `.github`
    as a gitlink here, the table test itself must fail. (Without it, the call
    could be deleted from the table test and every case below would still pass.)"""
    _private_workflows()
    monkeypatch.setattr(sys.modules[__name__], "_recorded", lambda root: {".github": ("160000", "1" * 40)})
    with pytest.raises(AssertionError, match=r"Recorded and not in the table: \['\.github \(a commit of"):
        test_every_workflow_file_is_exactly_as_pinned()


def test_without_a_git_repository_the_check_fails_and_never_skips(copied):
    """The folder is right and there is no repository to read: `copied` is a
    plain folder. The check must FAIL. It must not skip: a skip here would let
    the whole second half go unread wherever the repository cannot be found."""
    _assert_as_pinned(_on_disk(copied))
    try:
        _assert_folder_and_git_as_pinned(copied)
    except pytest.skip.Exception:
        # caught on purpose: left alone, a skip raised here would mark THIS test
        # as skipped, and the very thing it guards against would pass unseen
        pytest.fail("with no git repository the check was skipped; it must fail")
    except AssertionError as e:
        assert "is not the top of a git repository" in str(e) and "not a skip" in str(e), str(e)
    else:
        pytest.fail("with no git repository the check passed")
    # a folder INSIDE another repository is not its top either: that repository's
    # index would be read for a path it has nothing under
    _git_made_up(copied, "init", "-q")
    inner = copied / "unpacked"
    shutil.copytree(copied / ".github", inner / ".github")
    with pytest.raises(AssertionError, match="is not the top of a git repository"):
        _assert_recorded_as_pinned(inner)


def test_github_recorded_as_another_repositorys_commit_fails(recorded_copy):
    """The folder still holds the same five files, byte for byte, and every
    test that reads the folder passes. Git records one entry: `.github`, a
    commit of some other repository. This repository then holds no workflow."""
    _git_made_up(recorded_copy, "rm", "-r", "-q", "--cached", "--", ".github")
    _git_made_up(recorded_copy, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},.github")
    _assert_as_pinned(_on_disk(recorded_copy))            # the folder alone sees nothing wrong
    with pytest.raises(AssertionError, match=r"Recorded and not in the table: \['\.github \(a commit of "
                                             r"another repository in place of a folder \(a gitlink\)\)'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def test_a_workflow_recorded_as_a_link_fails(recorded_copy):
    """In the folder ercot.yml is a regular file with the pinned bytes. Git
    records a link at its path."""
    blob = _recorded(recorded_copy)[".github/workflows/ercot.yml"][1]
    _git_made_up(recorded_copy, "update-index", "--cacheinfo", f"120000,{blob},.github/workflows/ercot.yml")
    _assert_as_pinned(_on_disk(recorded_copy))
    with pytest.raises(AssertionError, match=r"does not record a regular file at: "
                                             r"\['\.github/workflows/ercot\.yml \(a link\)'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def test_a_workflow_whose_recorded_bytes_differ_from_the_file_fails(recorded_copy):
    """Other bytes are staged, then the file is put back as it was: the folder
    shows the pinned bytes and git records different ones."""
    workflow = recorded_copy / ".github" / "workflows" / "jp.yml"
    pinned = workflow.read_bytes()
    workflow.write_bytes(pinned + b"# one more line\n")
    _git_made_up(recorded_copy, "add", "--", ".github/workflows/jp.yml")
    staged = _git_plain(recorded_copy, "hash-object", "--no-filters", "--", ".github/workflows/jp.yml").strip()
    assert _recorded(recorded_copy)[".github/workflows/jp.yml"][1] == staged, (
        "git did not record the bytes this test wrote: this machine's git settings got in")
    workflow.write_bytes(pinned)
    _assert_as_pinned(_on_disk(recorded_copy))
    with pytest.raises(AssertionError, match=r"records other bytes than the working file holds at: "
                                             r"\['\.github/workflows/jp\.yml'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def test_a_filter_between_the_file_and_what_git_records_fails(recorded_copy):
    """A clean filter rewrites a file on its way into git. The folder shows the
    pinned bytes; git records the filter's output. The comparison must be with
    the file's own bytes (`--no-filters`): asked the ordinary way, git would
    run the same filter again and report that nothing differs."""
    _git_made_up(recorded_copy, "config", "filter.shout.clean", "tr a-z A-Z")
    (recorded_copy / ".git" / "info").mkdir(exist_ok=True)
    (recorded_copy / ".git" / "info" / "attributes").write_text(".github/workflows/ci.yml filter=shout\n")
    _git_made_up(recorded_copy, "rm", "-q", "--cached", "--", ".github/workflows/ci.yml")
    _git_made_up(recorded_copy, "add", "--", ".github/workflows/ci.yml")
    _assert_as_pinned(_on_disk(recorded_copy))
    with pytest.raises(AssertionError, match=r"records other bytes than the working file holds at: "
                                             r"\['\.github/workflows/ci\.yml'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def test_anything_else_recorded_under_github_fails(recorded_copy):
    """Recorded by git and then removed from the folder, so that no test that
    lists the folder can see it. (An action's own folder, a second workflows
    folder, a settings file: whatever it is, it needs a line in the table.)"""
    extra = recorded_copy / ".github" / "actions" / "push" / "action.yml"
    extra.parent.mkdir(parents=True)
    extra.write_text("runs: {using: composite, steps: []}\n")
    _git_made_up(recorded_copy, "add", "--", ".github/actions/push/action.yml")
    shutil.rmtree(recorded_copy / ".github" / "actions")
    _assert_as_pinned(_on_disk(recorded_copy))
    with pytest.raises(AssertionError, match=r"Recorded and not in the table: "
                                             r"\['\.github/actions/push/action\.yml \(a regular file\)'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def test_a_workflow_in_the_folder_that_git_does_not_record_fails(recorded_copy):
    """The file is there with the pinned bytes; it was never added (or was
    removed from the index). On GitHub it does not exist."""
    _git_made_up(recorded_copy, "rm", "-q", "--cached", "--", ".github/workflows/liveness.yml")
    _assert_as_pinned(_on_disk(recorded_copy))
    with pytest.raises(AssertionError, match=r"In the table and not recorded: "
                                             r"\['\.github/workflows/liveness\.yml'\]"):
        _assert_recorded_as_pinned(recorded_copy)


def _grants_write(text: str) -> bool:
    """Does this workflow give its own token write access to the repository's
    contents? Not a guess at intent from the words of its steps: this is the
    capability GitHub enforces. The repository's default for a workflow's token
    is read (checked 2026-11-13), so the token can write only where a workflow
    says so: `contents: write` under a `permissions:` key at any level, in
    block or one-line form, or `permissions: write-all`, with or without
    quotes around the key or the value and with any space around the colon.

    This is NOT the guard. The table of hashes is the guard; this is a check
    that the table's writers column is consistent with the files. It reads
    text, so it has limits: a grant written in a way it does not know (KNOWN
    MISSES INCLUDE: a YAML alias, `contents: *w`, whose value is written
    elsewhere, and the anchored value it points to; a folded or literal block
    value (`contents: >-` with `write` on the next line); a type tag
    (`contents: !!str write`); an escape inside double quotes
    (`"wr\\u0069te"`)), or a
    workflow that writes with a credential other than its own token (a key or
    a token kept in a secret), would leave a writer unregistered and its
    commit step untested. Any such change still changes the file's hash, and
    so comes before the readers. And it rests on the repository's default
    token permission staying `read`: if that setting became `write`, a file
    that says nothing would be a writer without a line changing.

    It errs the safe way in one case: the words `contents: write` in a comment
    at the end of a line, or in a command, count as a grant. That makes the
    check fail for a file that grants nothing, and a person then looks."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    return bool(re.search(r"""\bcontents['"]?\s*:\s*['"]?write\b"""
                          r"""|\bpermissions['"]?\s*:\s*['"]?write-all\b""", code))


def test_the_writers_in_the_table_are_the_workflows_that_may_write():
    """The behaviour tests in this file run for the table's writers and no
    others. So the table must be right about who they are: a workflow that may
    write and is not marked a writer would have no behaviour test at all, and
    one dropped from the list would lose its tests silently. Each writer must
    also have the one commit step those tests run."""
    _private_workflows()
    may_write = sorted(p.name for p in _workflow_files() if _grants_write(p.read_text()))
    assert may_write == sorted(WRITER_WORKFLOWS), (
        f"the workflows whose token may write to the repository are {may_write}; the "
        f"table marks {sorted(WRITER_WORKFLOWS)} as writers. Mark each one in PINNED "
        f"with the market it writes, so the real-git tests cover its commit step")
    assert sorted(WRITER_WORKFLOWS) == ["ercot.yml", "jp.yml"], (
        "the set of writers changed: a new writer needs the operator's go, and the "
        "behaviour tests must be seen to run for it")
    for workflow in WRITER_WORKFLOWS:
        assert _commit_step(workflow).strip()


@pytest.mark.parametrize("text, grants", [
    ("permissions:\n  contents: write\n", True),
    ("jobs:\n  tick:\n    permissions:\n      contents: write\n", True),          # a job's own
    ("permissions: {contents: write}\n", True),
    ("permissions: {issues: write, contents:   write}\n", True),
    ("permissions: write-all\n", True),
    ("permissions:\n  contents: read\n", False),
    ("permissions:\n  actions: read\n  issues: write\n  contents: read\n", False),
    ("on:\n  push:\n    branches: [main]\njobs:\n  test:\n    steps: []\n", False),  # says nothing: read
    ("# permissions:\n#   contents: write\npermissions:\n  contents: read\n", False),
    ("permissions:\n  contents: writeable\n", False),
    # quoted and spaced spellings GitHub reads the same way
    ('permissions:\n  contents: "write"\n', True),
    ("permissions:\n  contents: 'write'\n", True),
    ('permissions:\n  "contents": write\n', True),
    ("permissions:\n  'contents': 'write'\n", True),
    ("permissions:\n  contents : write\n", True),
    ("permissions:\n  contents:write\n", True),      # not a grant to YAML; counted, the safe way
    ('permissions: "write-all"\n', True),
    ("permissions : 'write-all'\n", True),
    ('"permissions": write-all\n', True),
    ("jobs:\n  tick:\n    permissions: write-all\n", True),                     # a job's own
    ('permissions:\n  contents: "writeable"\n', False),
    ('permissions:\n  contents: "read"\n', False),
    # counted though they grant nothing: the check fails and a person looks
    ("permissions:\n  issues: write  # not contents: write\n", True),
    ("steps:\n  - run: echo contents: write\n", True),
    # KNOWN MISSES, stated in _grants_write. Each of these IS a grant to GitHub.
    ("x: &w write\npermissions:\n  contents: *w\n", False),                    # behind an alias
    ("permissions:\n  contents: >-\n    write\n", False),                      # a folded block value
    ("permissions:\n  contents: !!str write\n", False),                        # a type tag
    ('permissions:\n  contents: "wr\\u0069te"\n', False),                      # an escape in quotes
])
def test_what_counts_as_a_grant_of_write_access(text, grants):
    assert _grants_write(text) is grants


def test_actions_writers_never_let_uv_rewrite_the_lock():
    """Measured 2026-11-13 (uv 0.12.7): with pyproject.toml one step ahead of
    uv.lock, a plain `uv sync --no-dev` and a plain `uv run --no-dev` each
    rewrite uv.lock; with UV_FROZEN=1 neither does. A rewritten lock is a dirty
    tracked file, and since the pull no longer stashes, it would cost the
    receipt at every rung of the ladder, not one. The tick has no runtime
    dependencies, so installing from the lock as committed loses nothing."""
    _private_workflows()
    for wf in sorted(WRITER_WORKFLOWS):
        code = "\n".join(_code_lines(WORKFLOWS / wf))
        uv_lines = [l.strip() for l in code.splitlines() if re.search(r"\buv (sync|run)\b", l)]
        assert uv_lines, f"{wf}: no uv command found; this guard reads nothing"
        assert re.search(r'^    env:\n(?:      .*\n)*?      UV_FROZEN: "1"$', code, re.M), (
            f"{wf}: {uv_lines} may rewrite uv.lock on the runner; set "
            f'UV_FROZEN: "1" in the job\'s env')
        assert code.count("UV_FROZEN") == 1, (
            f"{wf}: UV_FROZEN appears somewhere besides the job's env line. A step "
            f"that sets it again (`UV_FROZEN= uv sync`, a step-level env) undoes it")
        assert "--no-frozen" not in code
