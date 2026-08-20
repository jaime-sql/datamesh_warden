"""StateManager contract shared by the in-memory and Firestore backends.

Both `InMemoryStateManager` and `FirestoreStateManager` satisfy this Protocol
structurally (no inheritance needed); `app/persistence/factory.py` decides
which one to hand out based on `Settings.warden_mode`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.models.state import (
    AgentStepLog,
    DiagnosticFinding,
    GovernanceAudit,
    IncidentState,
    SQLPatchPayload,
)


def format_step_id(sequence: int) -> str:
    """Zero-pad a 1-based step sequence number for lexicographic ordering."""
    return f"{sequence:04d}"


class IncidentNotFoundError(KeyError):
    """Raised when an incident_id has no corresponding IncidentState."""


class StateManager(Protocol):
    async def create_incident(self, state: IncidentState) -> None: ...

    async def update_incident(self, incident_id: str, **patch: object) -> IncidentState: ...

    async def get_incident(self, incident_id: str) -> IncidentState: ...

    async def append_step(self, incident_id: str, log: AgentStepLog) -> None: ...

    async def write_finding(self, incident_id: str, finding: DiagnosticFinding) -> None: ...

    async def write_patch(self, incident_id: str, patch: SQLPatchPayload) -> None: ...

    async def write_audit(self, incident_id: str, audit: GovernanceAudit) -> None: ...

    def stream_steps(self, incident_id: str) -> AsyncIterator[AgentStepLog]: ...
