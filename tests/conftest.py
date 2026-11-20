"""Which repository is this, and what may a test take for granted?

`tests/` exists in two places. In the private operations repository it sits
beside everything it checks. On the public mirror it is copied with `src/`,
`scripts/` and a named list of documents, and NOT with the constitution, the
runbooks or the workflows (the list is `scripts/mirror_tree.sh`).

The public README says the suite can be run from the mirror. So a test that
needs a file the mirror does not carry must say so and be skipped there, with
the file named. In the private repository nothing may be skipped for that
reason: a missing file there is a failure.

The two are told apart by a file, `PRIVATE-REPO`, that only the private
repository has. Not by an environment variable (anyone can set one), and not by
one of the files the tests need (deleting it would then turn failures into
skips unnoticed). The private repository's CI checks the marker is present in
the checkout and absent from the tree built for the mirror.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKER = ROOT / "PRIVATE-REPO"


def is_private_repo() -> bool:
    return MARKER.exists()


def published_file(private_rel: str, mirror_rel: str) -> Path:
    """A file the mirror carries under ANOTHER name (`README-public.md` is its
    `README.md`; `mirror/verify.yml` is its `.github/workflows/verify.yml`).
    Returns whichever of the two this copy has, so the test runs in both places;
    if neither is here, the name this copy should have, so that reading it fails."""
    here, there = ROOT / private_rel, ROOT / mirror_rel
    if here.exists():
        return here
    if there.exists() and not is_private_repo():
        return there
    return here if is_private_repo() else there


def private_file(rel: str) -> Path:
    """The path of a file that only the private repository has.

    On a public copy, where it is absent, the calling test is skipped and the
    reason names the file. In the private repository the path is returned
    whether or not the file exists, so that reading a missing one fails."""
    path = ROOT / rel
    if not path.exists() and not is_private_repo():
        pytest.skip(f"needs {rel}, which only the private operations repository has")
    return path


# --- how GitHub Actions runs a `run:` step (register T-41, found by the PO) ---
# With no `shell:` (and no defaults.run.shell) GitHub runs the script as
# `bash -e {0}`: errexit, but NO pipefail, so `a | b` takes b's status. With
# `shell: bash` it runs `bash --noprofile --norc -eo pipefail {0}`. A test that
# runs a step's script must run it the way GitHub would, or it shares the very
# assumption it should test.
GITHUB_BASH = {None: ["bash", "-e", "-c"], "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]}


def workflow_steps(text: str) -> list[dict]:
    """The steps of a workflow file, in order, each as {"name", "shell",
    "run"}: the step's `shell:` value (None when absent) and its `run:` script
    (a one-line value, or a `run: |` block, dedented; None for a `uses:` step).
    Read from the text (the project has no YAML parser); the workflow tests pin
    the files, so the layout they read is fixed."""
    import re
    import textwrap
    lines = text.splitlines()
    steps, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)- (name|uses|run):", lines[i])
        if not m or not any(l.strip() == "steps:" for l in lines[:i]):
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
            j += 1
        block = [lines[i].replace("- ", "  ", 1)] + lines[i + 1:j]
        step = {"name": None, "shell": None, "run": None}
        k = 0
        while k < len(block):
            km = re.match(r"^(\s*)(name|shell|run):\s*(.*)$", block[k])
            if km and len(km.group(1)) == indent + 2:
                key, value = km.group(2), km.group(3).split(" #")[0].strip()
                if key == "run" and value in ("|", "|-"):
                    body = []
                    k += 1
                    while k < len(block) and (not block[k].strip() or
                                              len(block[k]) - len(block[k].lstrip()) > indent + 2):
                        body.append(block[k])
                        k += 1
                    step["run"] = textwrap.dedent("\n".join(body)).strip("\n") + "\n"
                    continue
                step[key] = value
            k += 1
        steps.append(step)
        i = j
    return steps


def github_shell(step: dict) -> list[str]:
    """The argv GitHub runs this step's script with (the script is its last
    argument). Only bash is used in this repository's workflows."""
    if step["shell"] not in GITHUB_BASH:
        raise AssertionError(f"step {step['name']!r}: shell {step['shell']!r} is not one these tests model")
    return GITHUB_BASH[step["shell"]]
