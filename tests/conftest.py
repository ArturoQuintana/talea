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
