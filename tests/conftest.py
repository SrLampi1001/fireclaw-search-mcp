"""Shared pytest fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache_dir(monkeypatch) -> Path:
    """Per-test cache dir, env-pointed at it, cleaned up after."""
    with tempfile.TemporaryDirectory(prefix="fireclaw-cache-") as d:
        path = Path(d)
        monkeypatch.setenv("FIRECRAWL_CACHE_DIR", str(path))
        yield path


@pytest.fixture
def isolated_env(monkeypatch):
    """Reset the env to a known clean state for tests that read settings."""
    for key in list(os.environ):
        if key.startswith(
            ("FIRECRAWL_", "FIRECLAW_", "MCP_LOG_LEVEL", "FASTMCP_LOG_LEVEL")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key-for-unit-tests")
    yield


@pytest.fixture(autouse=True)
def _per_test_cache(monkeypatch, tmp_path):
    """Every test gets a fresh cache directory.

    Without this, integration tests would share `./cache/firecrawl/` with
    previous pytest runs and could flake on a stale cache hit.
    """
    monkeypatch.setenv("FIRECRAWL_CACHE_DIR", str(tmp_path / "firecrawl"))
