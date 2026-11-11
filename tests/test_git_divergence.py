"""Regression tests for incident 2026-11-03..09 (independent auditor, 2026-11-09):
the server's private-repo clone DIVERGED from origin — a push was rejected while
the ERCOT/JP GitHub Actions writers pushed to main, and from then on every
pre-tick `git pull --ff-only` failed and every git_backup push was rejected
again. ES/DE/GB stopped reaching origin and ERCOT stopped reaching the server
(hence the public mirror: page "as of 11-08", data stopped 11-03).

These run REAL git against a bare origin with two writers on disjoint
Data/<slug>/ paths — the way the bug actually happened."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import talea.__main__ as cli

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> str:
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
