"""One Python everywhere. Until 2026-11-10 the Mac ran 3.13, CI and the two
GitHub Actions writers took whatever uv picked (3.14 by then), the public
mirror's verify job pinned 3.13, and the server ran Ubuntu's 3.12.3 — and the
P&L math rounds floats, where interpreters have differed before (incident
2026-08-29: `sum()` changed in 3.12, moving a value across a rounding boundary).
`.python-version` is the single pin; uv honours it in every `uv sync`/`uv run`,
so the Mac, CI, the Actions writers and the server all follow it."""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from conftest import private_file, published_file

ROOT = Path(__file__).resolve().parents[1]
PIN = (ROOT / ".python-version").read_text().strip()


def test_the_pin_is_one_exact_version_inside_requires_python():
    assert re.fullmatch(r"3\.\d+\.\d+", PIN), \
        f".python-version must name ONE exact release (X.Y.Z), not a range: {PIN!r}"
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    m = re.fullmatch(r">=(\d+)\.(\d+)", floor)
    assert m, f"requires-python changed shape ({floor!r}) — re-check it still admits the pin"
    assert tuple(map(int, PIN.split(".")[:2])) >= tuple(map(int, m.groups()))


def test_this_run_uses_the_pinned_interpreter():
    """Fails wherever the suite runs on another Python — which is the point:
    `uv run pytest` follows the pin, so a mismatch means something bypassed it."""
    running = ".".join(map(str, sys.version_info[:3]))
    assert running == PIN, (
        f"tests are running on Python {running}, but .python-version pins {PIN} — "
        f"run them with `uv run pytest`")


def test_every_workflow_gets_python_from_uv_and_the_pin():
    """One mechanism: uv reads .python-version. A workflow may not name a version
    of its own, pass --python to uv, or install Python with actions/setup-python
    — which had no build of the pinned release for the current runner image and
    failed red-main's first run (2026-11-11); the same change was one publish
    away from breaking the public mirror's verify job."""
    private_file(".github/workflows/ci.yml")          # the private repository's workflows
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + [ROOT / "mirror" / "verify.yml"]
    assert len(files) >= 5
    for f in files:
        text = f.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert "setup-python" not in code, \
            f"{f.name} installs Python with actions/setup-python — use setup-uv and the pin"
        assert "astral-sh/setup-uv" in code, f"{f.name} does not set up uv"
        assert not re.search(r"python-version:", code), \
            f"{f.name} names its own python-version; .python-version is the only pin"
        assert not re.search(r"uv (?:run|sync|python install)[^\n]*(?:--python|-p )", code), \
            f"{f.name} passes its own --python to uv instead of following .python-version"
        for line in code.splitlines():
            if re.search(r"\bpython3? +(scripts/|-m )", line):
                assert "uv run" in line, (
                    f"{f.name} runs Python outside uv, so the pin does not apply: {line.strip()}")


def test_the_pin_travels_to_the_public_mirror():
    """The mirror publishes the tests and tells readers to run `uv run pytest`;
    without the pin file there, that would run on whatever Python they have."""
    assert ".python-version" in (ROOT / "scripts" / "mirror_tree.sh").read_text()


def test_the_mirrors_own_workflow_follows_the_pin_too():
    """The one workflow the mirror does carry (verify.yml). Runs in both places:
    in the private repository it is mirror/verify.yml, on the mirror
    .github/workflows/verify.yml."""
    text = published_file("mirror/verify.yml", ".github/workflows/verify.yml").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "setup-python" not in code and "astral-sh/setup-uv" in code
    assert not re.search(r"python-version:", code)
