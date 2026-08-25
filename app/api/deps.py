"""FastAPI dependency-injection seams for Phase 4's HTTP surface.

`get_orchestrator` exists purely so tests can override it via
`app.dependency_overrides[get_orchestrator]` -- e.g. injecting a
`WardenOrchestrator` wired to a fake Gemini client -- without touching real
credentials or GCP. `app.persistence.factory.get_state_manager` is already a
plain function and is depended on directly (no wrapper needed here); it's
just as overridable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.agents.orchestrator import WardenOrchestrator

_background_tasks: set[asyncio.Task[None]] = set()


def get_orchestrator() -> WardenOrchestrator:
    """Factory for the orchestrator that processes a newly-ingested
    incident. Overridable via FastAPI's `dependency_overrides` in tests."""
    return WardenOrchestrator()


def run_in_background(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Fire-and-forget a coroutine while keeping a strong reference to its
    Task so it can't be silently garbage-collected mid-run -- a well-known
    asyncio footgun (see the "Important" note on `asyncio.create_task`).
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def drain_background_tasks() -> None:
    """Test-only helper: await every currently-tracked background task so
    assertions can run right after a fire-and-forget orchestrator run
    finishes, instead of sleeping/polling for it."""
    pending = list(_background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
