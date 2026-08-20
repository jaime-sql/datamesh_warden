from __future__ import annotations

from collections.abc import Generator

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_default_to_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WARDEN_MODE", raising=False)
    settings = get_settings()
    assert settings.warden_mode == "local"
    assert settings.warden_max_turns == 8
    assert settings.warden_orchestrator_model == "gemini-3.1-pro-preview"


def test_settings_reads_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARDEN_MODE", "cloud")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("WARDEN_MAX_TURNS", "3")

    settings = get_settings()

    assert settings.warden_mode == "cloud"
    assert settings.google_cloud_project == "test-project"
    assert settings.warden_max_turns == 3
