"""Chooses the StateManager implementation based on `Settings.warden_mode`.

`FirestoreStateManager` is imported lazily inside the "cloud" branch so
importing this module (or running in local mode) never requires GCP
Application Default Credentials to be configured.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.persistence.base import StateManager
from app.persistence.in_memory import InMemoryStateManager


@lru_cache
def get_state_manager() -> StateManager:
    settings = get_settings()
    if settings.warden_mode == "cloud":
        from app.persistence.firestore import FirestoreStateManager

        return FirestoreStateManager()
    return InMemoryStateManager()
