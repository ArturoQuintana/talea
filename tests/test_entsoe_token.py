"""The ENTSO-E token's .env fallback must read the REPO-ROOT .env.

Regression for incident 2026-10-01: when the shared A44 library moved from
src/<pkg>/fetch_entsoe.py to src/<pkg>/markets/_entsoe.py (2026-08-28), its
depth-relative `parents[2] / ".env"` silently became `src/.env`. On the server
the token lives ONLY in the repo-root .env (never exported by systemd), so every
IT/PT/FR fetch raised "ENTSOE_TOKEN not set" — caught as a feed-shaped failure,
degraded to fetch_error, and the three shadows went dark for 33 days. No test
pinned the fallback path; these do.
"""
from __future__ import annotations

import pytest

import talea.__main__ as cli
from talea.markets import _entsoe


def test_env_file_is_the_repo_root_dotenv():
    # Fails the way the real bug did: a depth-relative path that lands in src/.
    assert (_entsoe.ENV_FILE.parent / "pyproject.toml").is_file()
    assert _entsoe.ENV_FILE == cli.REPO / ".env"          # same file the shell reads


def test_token_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTSOE_TOKEN", "from-env")
    monkeypatch.setattr(_entsoe, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("ENTSOE_TOKEN=from-file\n")
    assert _entsoe._token() == "from-env"


def test_token_falls_back_to_the_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ENTSOE_TOKEN", raising=False)
    monkeypatch.setattr(_entsoe, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text('OTHER=x\nENTSOE_TOKEN="tok-from-file"\n')
    assert _entsoe._token() == "tok-from-file"


def test_token_missing_everywhere_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ENTSOE_TOKEN", raising=False)
    monkeypatch.setattr(_entsoe, "ENV_FILE", tmp_path / ".env")   # absent file
    with pytest.raises(RuntimeError, match="ENTSOE_TOKEN not set"):
        _entsoe._token()
    (tmp_path / ".env").write_text("OTHER=x\n")                   # file without the key
    with pytest.raises(RuntimeError, match="ENTSOE_TOKEN not set"):
        _entsoe._token()
