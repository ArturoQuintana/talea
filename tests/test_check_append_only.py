"""scripts/check_append_only.py: a pull request may append to a receipts.jsonl or
ledger.jsonl and may do nothing else to one (invariant 2: the audit trail is
never edited, revised or regenerated).

Real git, in throwaway repositories: a `main` with two markets' files, and a
branch that does to them what a bad pull request would."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_SPEC = importlib.util.spec_from_file_location(
    "check_append_only", ROOT / "scripts" / "check_append_only.py")
cao = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cao)

RECEIPTS = "Data/es/receipts.jsonl"
LEDGER = "Data/es/ledger.jsonl"
THREE = '{"n": 1}\n{"n": 2}\n{"n": 3}\n'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode())


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """`main` holding ES and JP receipts and ledgers; HEAD on a new branch `pr`."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    for rel in (RECEIPTS, LEDGER, "Data/jp/receipts.jsonl", "Data/jp/ledger.jsonl"):
        _write(r, rel, THREE)
    _write(r, "Data/es/prices.json", '{"2026-01-01T00": 1.0}\n')
    _commit(r, "seed")
    _git(r, "checkout", "-q", "-b", "pr")
    return r


def _check(repo: Path, capsys, base: str = "refs/heads/main", head: str = "HEAD") -> tuple[int, str]:
    code = cao.main(["--repo", str(repo), "--base", base, "--head", head])
    return code, capsys.readouterr().out


# ---- accepted ----

def test_an_appended_line_is_accepted(repo, capsys):
    _write(repo, RECEIPTS, THREE + '{"n": 4}\n')
    _write(repo, LEDGER, THREE + '{"n": 4}\n{"n": 5}\n')
    _commit(repo, "append")
    code, out = _check(repo, capsys)
    assert code == 0, out
    assert "4 receipts/ledger file(s)" in out
    assert "THIS CHANGE ADDS RECORD LINES: 2 file(s) appended to (+3 line(s)), 0 new file(s)" in out
    assert f"appended: {RECEIPTS} +1 line(s)" in out and f"appended: {LEDGER} +2 line(s)" in out


def test_a_change_that_leaves_the_audit_trail_alone_is_accepted(repo, capsys):
    """Prices, code and documents are not this check's business."""
    _write(repo, "Data/es/prices.json", '{"2026-01-01T00": 2.0}\n')
    _write(repo, "src/x.py", "x = 1\n")
    _commit(repo, "other files")
    code, out = _check(repo, capsys)
    assert code == 0 and "this change adds no receipt or ledger line" in out
    assert "ADDS RECORD LINES" not in out


def test_a_new_markets_files_are_accepted(repo, capsys):
    _write(repo, "Data/nz/receipts.jsonl", '{"n": 1}\n')
    _write(repo, "Data/nz/ledger.jsonl", "")
    _commit(repo, "new market")
    code, out = _check(repo, capsys)
    assert code == 0, out
    assert "0 file(s) appended to (+0 line(s)), 2 new file(s) (1 line(s))" in out
    assert "new: Data/nz/receipts.jsonl 1 line(s)" in out


def test_lines_a_branch_adds_itself_are_not_judged_but_are_reported(repo, capsys):
    """The stated guarantee (set in review, 2026-11-13): the check protects
    every line that is on main at the merge base. It does not judge how a branch
    arrived at lines it adds itself: append B, then replace B by C, and the tip
    `A, C` is accepted. So a passing run must SAY that record lines were added,
    for the readers of a pull request to see."""
    _write(repo, RECEIPTS, THREE + '{"n": "B"}\n')
    _commit(repo, "append B")
    _write(repo, RECEIPTS, THREE + '{"n": "C"}\n')
    _commit(repo, "replace B by C")
    code, out = _check(repo, capsys)
    assert code == 0, out
    assert "THIS CHANGE ADDS RECORD LINES: 1 file(s) appended to (+1 line(s)), 0 new file(s)" in out


# ---- refused: the three the rule names, and their near relations ----

def test_an_edited_line_is_refused(repo, capsys):
    _write(repo, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    _commit(repo, "doctor line 2")
    code, out = _check(repo, capsys)
    assert code == 1
    assert f"{LEDGER}: line 2 is not the line it was" in out
    assert RECEIPTS not in out, "only the rewritten file is named"


def test_an_edit_is_refused_even_when_lines_are_appended_after_it(repo, capsys):
    _write(repo, RECEIPTS, '{"n": 1}\n{"n": 2}\n{"n": 30}\n{"n": 4}\n')
    _commit(repo, "edit, then append")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: line 3" in out


@pytest.mark.parametrize("kept, said", [
    ('{"n": 1}\n{"n": 3}\n', "line 2 is not the line it was"),        # a middle line
    ('{"n": 1}\n{"n": 2}\n', "line(s) 3 to 3 are gone"),              # the last line
    ("", "line(s) 1 to 3 are gone"),                                  # emptied
])
def test_a_deleted_line_is_refused(repo, capsys, kept, said):
    _write(repo, RECEIPTS, kept)
    _commit(repo, "delete")
    code, out = _check(repo, capsys)
    assert code == 1 and RECEIPTS in out and said in out, out


def test_a_moved_file_is_refused(repo, capsys):
    """The same bytes at another path are still a rewrite of where the record lives."""
    (repo / "Data/spain").mkdir()
    _git(repo, "mv", RECEIPTS, "Data/spain/receipts.jsonl")
    _commit(repo, "move")
    code, out = _check(repo, capsys)
    assert code == 1
    assert f"{RECEIPTS}: is gone from its path (deleted or moved)" in out


def test_a_deleted_file_is_refused(repo, capsys):
    _git(repo, "rm", "-q", LEDGER)
    _commit(repo, "delete the ledger")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{LEDGER}: is gone from its path" in out


def test_reordered_lines_are_refused(repo, capsys):
    _write(repo, RECEIPTS, '{"n": 2}\n{"n": 1}\n{"n": 3}\n')
    _commit(repo, "reorder")
    assert _check(repo, capsys)[0] == 1


def test_a_line_inserted_before_the_end_is_refused(repo, capsys):
    _write(repo, RECEIPTS, '{"n": 1}\n{"n": 1.5}\n{"n": 2}\n{"n": 3}\n')
    _commit(repo, "insert")
    code, out = _check(repo, capsys)
    assert code == 1 and "line 2" in out


def test_a_change_of_bytes_inside_a_line_is_refused(repo, capsys):
    """Byte for byte: the same JSON re-serialised, or a changed line ending, is
    a different line (the OTS manifests hash these bytes)."""
    _write(repo, RECEIPTS, '{"n":1}\n{"n": 2}\n{"n": 3}\n')
    _commit(repo, "reformat")
    assert _check(repo, capsys)[0] == 1
    _write(repo, RECEIPTS, THREE[:-1])                     # final newline dropped
    _commit(repo, "drop the last newline")
    assert _check(repo, capsys)[0] == 1


def test_bytes_added_to_an_unterminated_last_line_are_refused(tmp_path, capsys):
    """If the last line had no newline at the merge base, 'appending' extends
    that line, which changes it."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    _write(r, RECEIPTS, '{"n": 1}\n{"n": 2}')
    _commit(r, "seed, torn last line")
    _git(r, "checkout", "-q", "-b", "pr")
    assert _check(r, capsys)[0] == 0                        # untouched: fine
    _write(r, RECEIPTS, '{"n": 1}\n{"n": 2}2\n')
    _commit(r, "extend the torn line")
    code, out = _check(r, capsys)
    assert code == 1 and "no line ending at the merge base" in out


def test_every_rewritten_file_is_listed_and_no_line_content_is_printed(repo, capsys):
    _write(repo, RECEIPTS, '{"n": 1}\n{"secret": 2}\n{"n": 3}\n')
    _write(repo, "Data/jp/ledger.jsonl", '{"n": 1}\n')
    _commit(repo, "two rewrites")
    code, out = _check(repo, capsys)
    assert code == 1 and "2 change(s) the audit trail does not allow" in out
    assert RECEIPTS in out and "Data/jp/ledger.jsonl" in out
    assert "secret" not in out and '"n"' not in out


# ---- the merge base, never main's tip ----

def _main_moves_on(repo: Path) -> None:
    """What the writers do several times a day: append to main after the branch was cut."""
    _git(repo, "checkout", "-q", "main")
    _write(repo, RECEIPTS, THREE + '{"n": 4}\n')
    _write(repo, LEDGER, THREE + '{"n": 4}\n')
    _commit(repo, "data: tick")
    _git(repo, "checkout", "-q", "pr")


def test_a_branch_behind_main_is_not_blamed_for_lines_main_gained(repo, capsys):
    """Compared with main's tip, an untouched branch looks as if it deleted the
    lines main has since appended. Compared with the merge base it is clean."""
    _write(repo, "src/x.py", "x = 1\n")
    _commit(repo, "code only")
    _main_moves_on(repo)
    code, out = _check(repo, capsys, base="refs/heads/main")
    assert code == 0, out
    assert _git(repo, "merge-base", "main", "HEAD")[:12] in out
    assert _git(repo, "rev-parse", "main")[:12] not in out


def test_a_branch_behind_main_is_still_refused_for_its_own_edit(repo, capsys):
    _write(repo, LEDGER, '{"n": 1}\n{"n": 2}\n{"n": 33}\n')
    _commit(repo, "doctor line 3")
    _main_moves_on(repo)
    code, out = _check(repo, capsys)
    assert code == 1 and f"{LEDGER}: line 3" in out


def test_a_branch_refreshed_by_merging_main_in_is_compared_with_what_it_merged(repo, capsys):
    """Refreshing by merge moves the merge base forward to the main commit that
    was merged. A clean branch stays clean; an edit made after the refresh, to a
    line main appended meanwhile, is still caught."""
    _write(repo, "src/x.py", "x = 1\n")
    _commit(repo, "code only")
    _main_moves_on(repo)
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert _check(repo, capsys)[0] == 0
    _write(repo, RECEIPTS, THREE + '{"n": 44}\n')
    _commit(repo, "doctor the line main appended")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: line 4" in out


# ---- a check that cannot be made is a failure, never a pass ----

def test_an_unknown_commit_cannot_be_checked(repo, capsys):
    code, out = _check(repo, capsys, base="refs/heads/no-such-ref")
    assert code == 2 and "CANNOT CHECK" in out


def test_unrelated_histories_cannot_be_checked(repo, capsys):
    _git(repo, "checkout", "-q", "--orphan", "island")
    _write(repo, "README", "x\n")
    _commit(repo, "unrelated root")
    code, out = _check(repo, capsys, base="refs/heads/main")
    assert code == 2 and "no merge base" in out


def test_a_shallow_clone_cannot_be_checked(repo, tmp_path, capsys):
    """CI's default checkout is one commit deep. There the merge base cannot be
    found, and the check must say so rather than pass on nothing."""
    main_tip = _git(repo, "rev-parse", "main")
    _write(repo, LEDGER, '{"n": 1}\n')
    head = _commit(repo, "delete two lines")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "pr",
                    f"file://{repo}", str(shallow)], check=True, capture_output=True)
    code, out = cao.main(["--repo", str(shallow), "--base", main_tip, "--head", head]), \
        capsys.readouterr().out
    assert code == 2 and "CANNOT CHECK" in out


def test_head_defaults_to_HEAD_and_repo_to_this_repository(repo, capsys, monkeypatch):
    _write(repo, LEDGER, '{"n": 1}\n')
    _commit(repo, "delete two lines")
    assert cao.main(["--repo", str(repo), "--base", "refs/heads/main"]) == 1
    capsys.readouterr()
    here = _git(ROOT, "rev-parse", "HEAD")
    assert cao.main(["--base", here]) == 0                  # this repository, against itself
    assert "receipts/ledger file(s)" in capsys.readouterr().out


def test_after_a_criss_cross_a_rewrite_seen_from_either_merge_base_is_refused(repo, capsys):
    """Two merge bases with DIFFERENT content: main appended a receipt (M1), the
    branch appended a ledger line (P1), and each then merged the other. The tip
    then replaces the receipt main appended and drops the ledger line. The
    receipt's line exists only at M1 and the ledger's only at P1, so whichever
    base git names first, one rewrite is visible only from the other. Both must
    be reported. (Seen from P1 alone the receipt change is a plain append.)"""
    _write(repo, LEDGER, THREE + '{"n": 4}\n'); p1 = _commit(repo, "pr: append to the ledger")
    _git(repo, "checkout", "-q", "main")
    _write(repo, RECEIPTS, THREE + '{"n": 4}\n'); m1 = _commit(repo, "main: append a receipt")
    _git(repo, "merge", "-q", "--no-edit", p1)
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", m1)
    assert set(_git(repo, "merge-base", "--all", "main", "HEAD").split()) == {m1, p1}
    assert _check(repo, capsys)[0] == 0
    _write(repo, RECEIPTS, THREE + '{"n": "not the line main appended"}\n')
    _write(repo, LEDGER, THREE)
    _commit(repo, "replace main's receipt, drop the ledger line")
    code, out = _check(repo, capsys)
    assert code == 1 and "2 change(s) the audit trail does not allow" in out, out
    assert f"{RECEIPTS}: line 4 is not the line it was at the merge base" in out
    assert f"{LEDGER}: has 3 line(s) where the merge base has 4" in out
    res = cao.compare(repo, "refs/heads/main", "HEAD")
    assert res.appended == {}, "a file rewritten against one base is not also an append"


def test_the_byte_rule_by_itself():
    same = b'{"n": 1}\n'
    assert cao.rewrite(same, same) is None
    assert cao.rewrite(same, same + b'{"n": 2}\n') is None
    assert cao.rewrite(b"", b'{"n": 1}\n') is None
    assert "not a whole line" in cao.rewrite(same, same + b" ")
    assert "line 1 is not" in cao.rewrite(same, b'{"n": 9}\n')
    assert "are gone" in cao.rewrite(same + same, same)


# ---- main, whatever the pull request targets ----

def test_a_branch_built_on_a_rewriting_branch_is_refused_against_main(repo, capsys):
    """Found in review: CI compared with the pull request's TARGET branch. A
    branch `bar` cut from a branch that holds a rewrite is clean against that
    branch, and stays green if the pull request is then pointed at main. The
    comparison is with main, always."""
    _write(repo, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    _commit(repo, "pr: doctor line 2")
    _git(repo, "checkout", "-q", "-b", "bar")
    _write(repo, "src/x.py", "x = 1\n")
    _commit(repo, "bar: code only")
    assert _check(repo, capsys, base="refs/heads/pr")[0] == 0, "against the branch it was cut from, nothing shows"
    code, out = _check(repo, capsys, base="refs/heads/main")
    assert code == 1 and f"{LEDGER}: line 2" in out


# ---- an ordinary file, at the merge base and at the tip ----

def test_a_symbolic_link_in_place_of_the_file_is_refused(repo, capsys):
    """Found in review: git stores a symbolic link as a blob whose bytes are the
    link's TARGET. A link whose target text begins with the file's old bytes
    passed the byte comparison. The entry's mode is part of the record."""
    (repo / RECEIPTS).unlink()
    os.symlink(THREE + '{"n": 4}\n', repo / RECEIPTS)       # target text = old bytes + a line
    _commit(repo, "a link whose target looks like an append")
    assert _git(repo, "ls-tree", "HEAD", "--", RECEIPTS).startswith("120000")
    code, out = _check(repo, capsys)
    assert code == 1, out
    assert f"{RECEIPTS}: is no longer an ordinary file: it is now a symbolic link" in out


def test_a_link_to_a_copy_of_the_file_is_refused(repo, capsys):
    _write(repo, "Data/es/copy.jsonl", THREE)
    (repo / LEDGER).unlink()
    os.symlink("copy.jsonl", repo / LEDGER)
    _commit(repo, "ledger becomes a link to a copy")
    code, out = _check(repo, capsys)
    assert code == 1 and "symbolic link" in out


def test_an_executable_bit_on_the_file_is_refused(repo, capsys):
    (repo / RECEIPTS).chmod(0o755)
    _commit(repo, "chmod +x")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: is no longer an ordinary file: it is now a file of mode 100755" in out


def test_a_submodule_link_in_place_of_the_file_is_refused(repo, capsys):
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "--cached", RECEIPTS)
    (repo / RECEIPTS).unlink()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},{RECEIPTS}")
    _git(repo, "commit", "-qm", "receipts become a submodule link")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: is no longer an ordinary file: it is now a submodule link" in out


def test_a_directory_in_place_of_the_file_is_refused(repo, capsys):
    _git(repo, "rm", "-q", RECEIPTS)
    _write(repo, RECEIPTS + "/part-1.jsonl", THREE)
    _commit(repo, "receipts become a directory")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: is no longer an ordinary file: it is now a directory" in out


def test_a_new_record_that_is_not_an_ordinary_file_is_refused(repo, capsys):
    _write(repo, "Data/nz/real.jsonl", '{"n": 1}\n')
    os.symlink("real.jsonl", repo / "Data/nz/receipts.jsonl")
    _commit(repo, "a new market whose receipts are a link")
    code, out = _check(repo, capsys)
    assert code == 1 and "Data/nz/receipts.jsonl: is a new record that is not an ordinary file" in out


def test_a_file_that_was_not_ordinary_at_the_merge_base_cannot_pass(tmp_path, capsys):
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    _write(r, "Data/es/real.jsonl", THREE)
    os.symlink("real.jsonl", r / RECEIPTS)
    _commit(r, "seed: receipts are a link")
    _git(r, "checkout", "-q", "-b", "pr")
    code, out = _check(r, capsys)
    assert code == 1 and "is a symbolic link at the merge base, so it cannot be compared" in out


# ---- whole lines only ----

def test_bytes_added_without_a_line_ending_are_refused(repo, capsys):
    """Found in review: three spaces appended to a receipts file passed this
    check and verify_ledger, and the writer's torn-line repair would later cut
    them off on main: a truncation of the record caused by a merged change."""
    _write(repo, RECEIPTS, THREE + "   ")
    _commit(repo, "trailing bytes, no newline")
    code, out = _check(repo, capsys)
    assert code == 1 and f"{RECEIPTS}: the bytes added at its end are not a whole line" in out
    _write(repo, RECEIPTS, THREE + '{"n": 4}')
    _commit(repo, "a whole record but no newline")
    assert _check(repo, capsys)[0] == 1


def test_a_new_file_must_end_with_a_line_ending(repo, capsys):
    _write(repo, "Data/nz/receipts.jsonl", '{"n": 1}')
    _commit(repo, "new, unterminated")
    code, out = _check(repo, capsys)
    assert code == 1 and "Data/nz/receipts.jsonl: is new and its last line has no line ending" in out


# ---- more ways a check cannot be made ----

def test_a_shallow_clone_cannot_be_checked_even_when_git_finds_a_merge_base(repo, tmp_path, capsys):
    """Found in review: with history cut off, both commits can exist and git
    still names a merge base, possibly an older one than the true one. Any
    shallow repository is refused before comparing."""
    _git(repo, "checkout", "-q", "main")
    for n in (1, 2):                                        # history deeper than the clone
        _write(repo, f"docs/{n}.md", "x\n")
        _commit(repo, f"main: commit {n}")
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--ff-only", "main")
    _write(repo, "src/x.py", "x = 1\n")
    _commit(repo, "one more commit on the branch")
    head, main_tip = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "main")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "2", "--no-single-branch",
                    f"file://{repo}", str(shallow)], check=True, capture_output=True)
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(shallow, "merge-base", main_tip, head) == main_tip, \
        "git does answer here; that is the trap"
    code = cao.main(["--repo", str(shallow), "--base", main_tip, "--head", head])
    out = capsys.readouterr().out
    assert code == 2 and "CANNOT CHECK" in out and "shallow clone" in out


def test_a_repository_with_no_guarded_file_cannot_be_checked(tmp_path, capsys):
    """`ok -- 0 file(s)` would be a pass on nothing: the wrong repository, the
    wrong folder, or a base with the files already gone."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    _write(r, "README", "x\n")
    _commit(r, "seed")
    _git(r, "checkout", "-q", "-b", "pr")
    code, out = _check(r, capsys)
    assert code == 2 and "Nothing was compared" in out


def test_git_missing_cannot_be_checked(repo, tmp_path, capsys, monkeypatch):
    """An OSError from the missing program was a traceback and exit 1, which
    reads as 'a rewrite was found'."""
    empty = tmp_path / "no-programs"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out = _check(repo, capsys)
    assert code == 2 and "git could not be run" in out


# ---- a directory named like a record ----

def test_a_new_folder_named_like_a_record_is_refused(repo, capsys):
    """Found in review: `Data/nz/receipts.jsonl/part-1.jsonl`. A plain recursive
    listing shows only the file inside, whose name is not guarded, so a folder
    standing where a market's receipts belong was invisible."""
    _write(repo, "Data/nz/receipts.jsonl/part-1.jsonl", '{"n": 1}\n')
    _commit(repo, "a folder where the receipts file should be")
    code, out = _check(repo, capsys)
    assert code == 1, out
    assert "Data/nz/receipts.jsonl: is a new record that is not an ordinary file: it is a directory" in out


def test_a_folder_named_like_a_record_at_the_merge_base_cannot_pass(tmp_path, capsys):
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    _write(r, LEDGER, THREE)
    _write(r, RECEIPTS + "/part-1.jsonl", THREE)
    _commit(r, "seed: receipts are a folder")
    _git(r, "checkout", "-q", "-b", "pr")
    code, out = _check(r, capsys)
    assert code == 1 and f"{RECEIPTS}: is a directory at the merge base, so it cannot be compared" in out


# ---- main must be named so that nothing else can answer to the name ----

@pytest.mark.parametrize("name", ["main", "origin/main", "HEAD", "HEAD~1", "refs/heads/main^",
                                  "abc1234", ""])
def test_a_base_that_is_not_a_full_ref_or_a_commit_id_cannot_be_checked(repo, capsys, name):
    code, out = _check(repo, capsys, base=name)
    assert code == 2 and "CANNOT CHECK" in out and "must be a full ref" in out


def test_a_name_that_looks_like_an_option_never_reaches_git(repo):
    for name in ("--upload-pack=touch x", "-refs/heads/main", "refs/heads/main --all"):
        with pytest.raises(cao.CannotCheck, match="must be a full ref"):
            cao.resolve(repo, name, what="base")


def test_the_head_may_be_HEAD_a_full_ref_or_a_commit_id_and_nothing_else(repo, capsys):
    sha = _git(repo, "rev-parse", "HEAD")
    for head in ("HEAD", "refs/heads/pr", sha):
        assert _check(repo, capsys, head=head)[0] == 0
    for head in ("pr", "HEAD~0", sha[:12]):
        assert _check(repo, capsys, head=head)[0] == 2


def _origin_and_clone(tmp_path: Path, *, script_on_main: bool) -> Path:
    """A bare origin whose main holds two markets' records (and, optionally,
    this repository's real check script), and a clone of it with HEAD on a new
    branch `pr`: what a pull request's checkout looks like."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.email", "t@t"); _git(seed, "config", "user.name", "t")
    for rel in (RECEIPTS, LEDGER):
        _write(seed, rel, THREE)
    if script_on_main:
        _write(seed, "scripts/check_append_only.py",
               (ROOT / "scripts" / "check_append_only.py").read_text())
    _commit(seed, "seed")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@t"); _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "pr")
    return work


def test_a_tag_named_like_the_remote_branch_cannot_stand_in_for_main(tmp_path, capsys):
    """Found in review: a tag called `origin/main`. git resolves the short name
    `origin/main` to the TAG and only prints a warning. Pointed at the pull
    request's own tip, it makes the merge base the tip itself, and everything
    compares clean. The full ref cannot be taken over, and the short name is
    no longer accepted at all."""
    work = _origin_and_clone(tmp_path, script_on_main=False)
    _write(work, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    head = _commit(work, "doctor line 2")
    _git(work, "tag", "origin/main", head)
    assert _git(work, "rev-parse", "origin/main") == head, "the trap: the short name is the tag now"
    code, out = _check(work, capsys, base="refs/remotes/origin/main", head=head)
    assert code == 1 and f"{LEDGER}: line 2" in out
    assert _check(work, capsys, base="origin/main", head=head)[0] == 2


def test_a_replace_ref_cannot_make_the_tip_look_like_the_merge_base(repo, capsys):
    """Found in review: `git replace <tip> <merge base>` makes git answer for the
    tip with the merge base's content. The check reads the real objects."""
    base = _git(repo, "rev-parse", "main")
    _write(repo, LEDGER, '{"n": 1}\n')
    head = _commit(repo, "delete two lines")
    _git(repo, "replace", head, base)
    assert _git(repo, "show", f"{head}:{LEDGER}") == THREE.strip(), "the trap: git now shows the old file"
    code, out = _check(repo, capsys, head=head)
    assert code == 1 and f"{LEDGER}: has 1 line(s) where the merge base has 3" in out


def test_under_github_actions_the_script_refuses_to_start_unless_isolated():
    """Run through the project's environment, a file in the change can be
    imported before the check runs. In CI it must be `python3 -I -S`."""
    from types import SimpleNamespace as Flags
    ci = {"GITHUB_ACTIONS": "true"}
    assert cao.interpreter_problem(ci, Flags(isolated=1, no_site=1)) is None
    assert "python3 -I -S" in cao.interpreter_problem(ci, Flags(isolated=0, no_site=0))
    assert cao.interpreter_problem(ci, Flags(isolated=1, no_site=0)), "-I alone leaves sitecustomize"
    assert cao.interpreter_problem(ci, Flags(isolated=0, no_site=1))
    assert cao.interpreter_problem({}, Flags(isolated=0, no_site=0)) is None, "a developer's shell"
    assert cao.interpreter_problem({"GITHUB_ACTIONS": "false"}, Flags(isolated=0, no_site=0)) is None


# ---- it is wired into CI: first, alone, from main's copy ----
#
# A pull request that edits ci.yml can still switch the check off: the workflow
# file that runs is the pull request's own, and that cannot be closed from
# inside its own CI. ci.yml is a reserved path for that reason. What this
# section does is (a) run the step exactly as ci.yml has it against the ways a
# pull request could defeat it WITHOUT touching ci.yml, and (b) make every known
# quiet edit of ci.yml fail the suite.

CI = ROOT / ".github" / "workflows" / "ci.yml"
needs_ci = pytest.mark.skipif(
    not CI.exists(),
    reason=".github/workflows/ci.yml is not in this copy (the public mirror carries "
           "tests/ but not the private repository's workflows)")

HEADER = """name: ci
on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened, edited]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
"""
CHECKOUT = ["- uses: actions/checkout@v4", "  with:", "    fetch-depth: 0"]
GUARD = [
    "- name: Existing receipt and ledger lines are unchanged (pull requests)",
    "  if: github.event_name == 'pull_request'",
    "  run: |",
    '    check="$RUNNER_TEMP/check_append_only.py"',
    '    if git cat-file -e "refs/remotes/origin/main:scripts/check_append_only.py" 2>/dev/null; then',
    '      git show "refs/remotes/origin/main:scripts/check_append_only.py" > "$check"',
    "    else",
    '      cp scripts/check_append_only.py "$check"',
    "    fi",
    '    python3 -I -S "$check" --repo "$GITHUB_WORKSPACE" --base refs/remotes/origin/main '
    '--head "${{ github.event.pull_request.head.sha }}"',
]
SHA_EXPR = "${{ github.event.pull_request.head.sha }}"


def _steps(text: str) -> tuple[list[str], list[list[str]]]:
    """(the workflow's lines without comments or blanks, its steps as lists of
    lines dedented to the step's dash)."""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    under_jobs = lines[lines.index("jobs:") + 1:] if "jobs:" in lines else []
    steps: list[list[str]] = []
    for l in under_jobs:
        if l.startswith("      - "):
            steps.append([l[6:]])
        elif l.startswith("        ") and steps:
            steps[-1].append(l[6:])
    return lines, steps


def _ci_problems(text: str) -> list[str]:
    """Everything about ci.yml that would let the append-only step be skipped,
    emptied, renamed, or reached after something else has run. Text-based
    (standard library only, no YAML reader), so it is strict: the parts that
    matter must be spelled exactly."""
    lines, steps = _steps(text)
    code = "\n".join(lines) + "\n"
    problems = []
    if not code.startswith(HEADER):
        problems.append("the name, the triggers (with `edited`), or the head of the one job "
                        "`test` are not exactly as pinned")
    top = [l for l in lines if not l.startswith(" ")]
    if top != ["name: ci", "on:", "jobs:"]:
        problems.append(f"top-level keys are {top}: a `defaults:` or `env:` can empty every step")
    under_jobs = lines[lines.index("jobs:") + 1:] if "jobs:" in lines else []
    jobs = [l for l in under_jobs if l.startswith("  ") and not l.startswith("   ")]
    if jobs != ["  test:"]:
        problems.append(f"jobs are {jobs}: exactly one, `test:`, in block style")
    job_keys = [l for l in under_jobs if l.startswith("    ") and not l.startswith("     ")]
    if job_keys != ["    runs-on: ubuntu-latest", "    steps:"]:
        problems.append(f"the job's own keys are {job_keys}: a `name:` renames the check, "
                        "an `if:` or `continue-on-error:` empties it, a matrix multiplies it")
    if steps[:2] != [CHECKOUT, GUARD]:
        problems.append("the job must BEGIN with exactly the full-history checkout and then "
                        "the append-only step: any step before the check, or between the two, "
                        "runs before it judges (it could move origin/main, add a replace ref, "
                        "or put another `git` on the PATH)")
    if sum(any("check_append_only" in l for l in s) for s in steps) != 1:
        problems.append("the append-only step is missing or duplicated")
    if sum(any("uv run pytest" in l for l in s) for s in steps) != 1:
        problems.append("there must be exactly one suite step")
    syncs = [l.strip() for l in lines if "uv sync" in l]
    if syncs != ["- run: uv sync --locked"]:
        problems.append(f"the install is {syncs}, not exactly `uv sync --locked`")
    for word in ("continue-on-error", "matrix", "strategy", "|| true", "[skip"):
        if word in code:
            problems.append(f"`{word}` appears in ci.yml")
    return problems


@needs_ci
def test_ci_runs_the_check_exactly_as_pinned():
    assert _ci_problems(CI.read_text()) == []


def _moved_after_suite(text: str) -> str:
    start = text.index("      - name: Existing receipt and ledger lines")
    end = text.index("\n", text.index("python3 -I -S", start)) + 1
    return text[:start] + text[end:].rstrip("\n") + "\n" + text[start:end]


def _step_before_guard(step: str):
    marker = "      - name: Existing receipt and ledger lines"
    return lambda t: t.replace(marker, step + marker)


LAST = SHA_EXPR + '"\n'
WEAKENINGS = {
    "the command prefixed with echo":
        lambda t: t.replace('          python3 -I -S "$check"', '          echo python3 -I -S "$check"'),
    "; true after the command":
        lambda t: t.replace(LAST, SHA_EXPR + '"; true\n'),
    "|| true after the command":
        lambda t: t.replace(LAST, SHA_EXPR + '" || true\n'),
    "&& false in the if":
        lambda t: t.replace("if: github.event_name == 'pull_request'",
                            "if: github.event_name == 'pull_request' && false"),
    "a second --head":
        lambda t: t.replace(LAST, SHA_EXPR + '" --head refs/remotes/origin/main\n'),
    "the base taken from the pull request's target":
        lambda t: t.replace("--base refs/remotes/origin/main",
                            '--base "${{ github.event.pull_request.base.sha }}"'),
    "the base given by its short name":
        lambda t: t.replace("--base refs/remotes/origin/main", "--base origin/main"),
    "run through the project's environment again":
        lambda t: t.replace('python3 -I -S "$check"', 'uv run python "$check"'),
    "isolated, but with site (sitecustomize would run)":
        lambda t: t.replace('python3 -I -S "$check"', 'python3 -I "$check"'),
    "always the pull request's own copy of the script":
        lambda t: t.replace('git show "refs/remotes/origin/main:scripts/check_append_only.py" > "$check"',
                            'cp scripts/check_append_only.py "$check"'),
    "continue-on-error on the step":
        lambda t: t.replace("        if: github.event_name == 'pull_request'\n",
                            "        if: github.event_name == 'pull_request'\n"
                            "        continue-on-error: true\n"),
    "another working directory for the step":
        lambda t: t.replace("        if: github.event_name == 'pull_request'\n",
                            "        if: github.event_name == 'pull_request'\n"
                            "        working-directory: /tmp\n"),
    "the step deleted": lambda t: _moved_after_suite(t)[:_moved_after_suite(t).index(
        "      - name: Existing receipt and ledger lines")],
    "the step moved after the suite": _moved_after_suite,
    "a step before it that moves origin/main to the pull request's tip": _step_before_guard(
        "      - run: git update-ref refs/remotes/origin/main \"$GITHUB_SHA\"\n"),
    "a step before it that adds a replace ref": _step_before_guard(
        "      - run: git replace HEAD refs/remotes/origin/main\n"),
    "a step before it that puts another git on the PATH": _step_before_guard(
        '      - run: echo "$GITHUB_WORKSPACE/bin" >> "$GITHUB_PATH"\n'),
    "the installer moved before it": _step_before_guard(
        "      - uses: astral-sh/setup-uv@v5\n"),
    "the checkout replaced by another action":
        lambda t: t.replace("- uses: actions/checkout@v4", "- uses: someone/checkout@v4"),
    "the checkout told to fetch another ref":
        lambda t: t.replace("          fetch-depth: 0\n", "          fetch-depth: 0\n          ref: main\n"),
    "a second job in flow style":
        lambda t: t.replace("jobs:\n", 'jobs:\n  extra: {runs-on: ubuntu-latest, '
                                        'steps: [{run: "true"}]}\n'),
    "a second job in block style":
        lambda t: t + "  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n",
    "the job given a display name":
        lambda t: t.replace("    runs-on: ubuntu-latest\n",
                            "    name: other\n    runs-on: ubuntu-latest\n"),
    "the job switched off":
        lambda t: t.replace("    runs-on: ubuntu-latest\n",
                            "    if: false\n    runs-on: ubuntu-latest\n"),
    "a matrix":
        lambda t: t.replace("    runs-on: ubuntu-latest\n",
                            "    strategy:\n      matrix:\n        py: [1, 2]\n"
                            "    runs-on: ubuntu-latest\n"),
    "a default shell that runs nothing":
        lambda t: t.replace("jobs:\n", "defaults:\n  run:\n    shell: 'true {0}'\njobs:\n"),
    "`edited` dropped from the triggers":
        lambda t: t.replace("types: [opened, synchronize, reopened, edited]",
                            "types: [opened, synchronize, reopened]"),
    "pull requests that touch Data/ not run at all":
        lambda t: t.replace("  pull_request:\n", "  pull_request:\n    paths-ignore: ['Data/**']\n"),
    "a one-commit checkout":
        lambda t: t.replace("fetch-depth: 0", "fetch-depth: 1"),
    "a plain uv sync":
        lambda t: t.replace("uv sync --locked", "uv sync"),
}


@needs_ci
@pytest.mark.parametrize("how", sorted(WEAKENINGS))
def test_the_ci_guard_catches_each_known_way_to_weaken_the_step(how):
    """Found in review, over three rounds: each of these left an earlier version
    of the workflow test green. Each is applied to the real ci.yml text here
    and must be reported."""
    real = CI.read_text()
    doctored = WEAKENINGS[how](real)
    assert doctored != real, f"the weakening {how!r} changed nothing; this case proves nothing"
    assert _ci_problems(doctored), f"not caught: {how}"


# ---- the step itself, run as ci.yml has it ----

def _run_ci_step(work: Path, tmp_path: Path, head: str) -> subprocess.CompletedProcess:
    """Run the append-only step's script exactly as written in ci.yml, the way
    Actions does (`bash -e`), in a pull request's checkout. Only the one
    `${{ … }}` expression is filled in, as Actions would fill it."""
    _, steps = _steps(CI.read_text())
    step = next(s for s in steps if any("check_append_only" in l for l in s))
    at = step.index("  run: |")
    script = "\n".join(l[4:] for l in step[at + 1:]) + "\n"
    assert SHA_EXPR in script
    temp = tmp_path / "runner-temp"
    temp.mkdir(exist_ok=True)
    env = {**os.environ, "RUNNER_TEMP": str(temp), "GITHUB_WORKSPACE": str(work),
           "GITHUB_ACTIONS": "true"}
    return subprocess.run(["bash", "-e", "-c", script.replace(SHA_EXPR, head)],
                          cwd=work, capture_output=True, text=True, env=env)


EXIT_ZERO_AT_ONCE = "import os\nos._exit(0)\n"


@needs_ci
def test_the_step_judges_with_mains_copy_of_the_script_not_the_pull_requests(tmp_path):
    """A pull request that rewrites a ledger line AND replaces the check script
    with one that always passes. Main's copy judges it."""
    work = _origin_and_clone(tmp_path, script_on_main=True)
    _write(work, "scripts/check_append_only.py", "import sys\nsys.exit(0)\n")
    _write(work, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    head = _commit(work, "doctor line 2, and neuter the check")
    r = _run_ci_step(work, tmp_path, head)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"{LEDGER}: line 2 is not the line it was" in r.stdout
    assert "isolated=1, no_site=1" in r.stdout and "runner-temp" in r.stdout


@needs_ci
def test_the_step_cannot_be_ended_by_a_module_the_pull_request_plants(tmp_path):
    """Found in review, by doing it: `scripts/argparse.py` (Python puts a
    script's own folder first on its import path) and `src/sitecustomize.py`
    (an editable install puts `src/` on the path), each ending the process with
    exit 0 before the check had compared anything. The whole suite still
    passed. Isolated, without site, from a temporary folder: neither loads."""
    work = _origin_and_clone(tmp_path, script_on_main=True)
    for rel in ("scripts/argparse.py", "scripts/subprocess.py", "src/sitecustomize.py",
                "sitecustomize.py", "argparse.py", "usercustomize.py"):
        _write(work, rel, EXIT_ZERO_AT_ONCE)
    _write(work, RECEIPTS, '{"n": 1}\n')
    head = _commit(work, "delete two receipts, and plant modules")
    r = _run_ci_step(work, tmp_path, head)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"{RECEIPTS}: has 1 line(s) where the merge base has 3" in r.stdout


@needs_ci
def test_before_main_has_the_script_the_step_runs_the_pull_requests_copy(tmp_path):
    """The one pull request that introduces the check has nothing on main to be
    judged by. Its own copy runs, and still refuses a rewrite."""
    work = _origin_and_clone(tmp_path, script_on_main=False)
    _write(work, "scripts/check_append_only.py",
           (ROOT / "scripts" / "check_append_only.py").read_text())
    clean = _commit(work, "add the check")
    r = _run_ci_step(work, tmp_path, clean)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "append-only: ok" in r.stdout and "this change adds no receipt or ledger line" in r.stdout
    _write(work, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    head = _commit(work, "doctor line 2")
    r = _run_ci_step(work, tmp_path, head)
    assert r.returncode == 1 and f"{LEDGER}: line 2" in r.stdout


@needs_ci
def test_the_step_is_not_fooled_by_a_tag_named_origin_main(tmp_path):
    work = _origin_and_clone(tmp_path, script_on_main=True)
    _write(work, LEDGER, '{"n": 1}\n{"n": 22}\n{"n": 3}\n')
    head = _commit(work, "doctor line 2")
    _git(work, "tag", "origin/main", head)
    r = _run_ci_step(work, tmp_path, head)
    assert r.returncode == 1 and f"{LEDGER}: line 2" in r.stdout


@needs_ci
def test_the_step_fails_when_main_cannot_be_found(tmp_path):
    """No `refs/remotes/origin/main` (a checkout that fetched something else):
    the step must fail, not fall back to passing."""
    work = _origin_and_clone(tmp_path, script_on_main=True)
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")
    _write(work, "scripts/check_append_only.py",
           (ROOT / "scripts" / "check_append_only.py").read_text())
    r = _run_ci_step(work, tmp_path, head)
    assert r.returncode == 2 and "CANNOT CHECK" in r.stdout
