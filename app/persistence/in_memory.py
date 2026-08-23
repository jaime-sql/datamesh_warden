"""Offline-dev StateManager: everything lives in process memory.

`stream_steps` fans out over per-incident `asyncio.Queue`s so the local
Streamlit UI can subscribe the same way it eventually will against
Firestore's `on_snapshot` watch, without needing any GCP credentials.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.models.state import (
    AgentStepLog,
    DiagnosticFinding,
    GovernanceAudit,
    IncidentState,
    SQLPatchPayload,
)
from app.persistence.base import IncidentNotFoundError, PatchNotFoundError


class InMemoryStateManager:
    def __init__(self) -> None:
        self._incidents: dict[str, IncidentState] = {}
        self._steps: dict[str, list[AgentStepLog]] = defaultdict(list)
        self._findings: dict[str, list[DiagnosticFinding]] = defaultdict(list)
        self._patches: dict[str, list[SQLPatchPayload]] = defaultdict(list)
        self._audits: dict[str, list[GovernanceAudit]] = defaultdict(list)
        self._subscribers: dict[str, list[asyncio.Queue[AgentStepLog | None]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def create_incident(self, state: IncidentState) -> None:
        async with self._lock:
            self._incidents[state.incident_id] = state

    async def update_incident(self, incident_id: str, **patch: object) -> IncidentState:
        async with self._lock:
            current = self._require_incident(incident_id)
            merged = {**patch, "updated_at": datetime.now(UTC)}
            updated = current.model_copy(update=merged)
            self._incidents[incident_id] = updated
            return updated

    async def get_incident(self, incident_id: str) -> IncidentState:
        async with self._lock:
            return self._require_incident(incident_id)

    async def append_step(self, incident_id: str, log: AgentStepLog) -> None:
        async with self._lock:
            self._require_incident(incident_id)
            self._steps[incident_id].append(log)
            for queue in self._subscribers[incident_id]:
                queue.put_nowait(log)

    async def write_finding(self, incident_id: str, finding: DiagnosticFinding) -> None:
        async with self._lock:
            self._require_incident(incident_id)
            self._findings[incident_id].append(finding)

    async def write_patch(self, incident_id: str, patch: SQLPatchPayload) -> None:
        async with self._lock:
            self._require_incident(incident_id)
            self._patches[incident_id].append(patch)

    async def get_patch(self, incident_id: str, patch_id: str) -> SQLPatchPayload:
        async with self._lock:
            self._require_incident(incident_id)
            for patch in self._patches[incident_id]:
                if patch.patch_id == patch_id:
                    return patch
        raise PatchNotFoundError(patch_id)

    async def write_audit(self, incident_id: str, audit: GovernanceAudit) -> None:
        async with self._lock:
            self._require_incident(incident_id)
            self._audits[incident_id].append(audit)

    async def stream_steps(self, incident_id: str) -> AsyncIterator[AgentStepLog]:
        self._require_incident(incident_id)
        queue: asyncio.Queue[AgentStepLog | None] = asyncio.Queue()
        for existing in self._steps[incident_id]:
            queue.put_nowait(existing)
        async with self._lock:
            self._subscribers[incident_id].append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            async with self._lock:
                self._subscribers[incident_id].remove(queue)

    async def close_stream(self, incident_id: str) -> None:
        """Push a sentinel so any `stream_steps` consumers stop cleanly."""
        async with self._lock:
            for queue in self._subscribers[incident_id]:
                queue.put_nowait(None)

    def list_findings(self, incident_id: str) -> list[DiagnosticFinding]:
        return list(self._findings[incident_id])

    def list_patches(self, incident_id: str) -> list[SQLPatchPayload]:
        return list(self._patches[incident_id])

    def list_audits(self, incident_id: str) -> list[GovernanceAudit]:
        return list(self._audits[incident_id])

    def list_steps(self, incident_id: str) -> list[AgentStepLog]:
        return list(self._steps[incident_id])

    def _require_incident(self, incident_id: str) -> IncidentState:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise IncidentNotFoundError(incident_id) from exc
