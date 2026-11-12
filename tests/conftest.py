"""Shared fixtures: hooks read CLAUDE_PROJECT_DIR / CLAUDE_PLUGIN_ROOT, so tests start without them."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(name, raising=False)
