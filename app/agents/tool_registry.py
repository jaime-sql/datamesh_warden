"""Single source of truth mapping tool names to their implementations and
their `google-genai` schema. Consumed by the Phase 3 orchestrator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from google.genai import types

from app.agents.tools.governance import verify_governance_policy
from app.agents.tools.investigate import investigate_incident_logs
from app.agents.tools.patch import generate_and_test_patch
from app.agents.tools.schemas import build_tools

TOOL_IMPL: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "investigate_incident_logs": investigate_incident_logs,
    "generate_and_test_patch": generate_and_test_patch,
    "verify_governance_policy": verify_governance_policy,
}

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_IMPL.keys())

TOOLS: list[types.Tool] = build_tools()
