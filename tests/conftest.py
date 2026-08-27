"""Test-suite-wide fixtures.

The suite must stay hermetic and credential-free regardless of a
developer's local `.env` file -- e.g. one configured for real
`WARDEN_MODE=cloud` testing against live GCP resources (see
docs/architecture.md's Phase 2/3/4 implementation notes on local vs.
cloud backends). `Settings.model_config["env_file"]` is disabled for the
whole test session so `get_settings()` only ever sees actual process
environment variables -- which individual tests still set via
`monkeypatch.setenv` exactly as before -- plus the class's hardcoded
defaults, never whatever happens to be sitting in `.env`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import Settings


@pytest.fixture(autouse=True, scope="session")
def _ignore_dotenv_in_tests() -> Iterator[None]:
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = original
