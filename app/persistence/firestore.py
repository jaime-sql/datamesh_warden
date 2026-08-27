"""Production StateManager backed by Firestore.

Only instantiated when `Settings.warden_mode == "cloud"` (see factory.py).
`google.cloud.firestore.AsyncClient()` resolves Application Default
Credentials eagerly in its constructor, so this module intentionally does
nothing at import time -- construction is deferred until cloud mode is
actually selected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore import AsyncClient, AsyncDocumentReference
from pydantic import TypeAdapter

from app.models.state import (
    AgentStepLog,
    DiagnosticFinding,
    GovernanceAudit,
    IncidentState,
    SQLPatchPayload,
)
from app.persistence.base import IncidentNotFoundError, PatchNotFoundError

_JSON_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])


class FirestoreStateManager:
    """Note: `stream_steps` only replays steps that already exist at call
    time. Real-time push updates for the Streamlit UI are implemented at the
    UI layer (see `ui/state_bridge.py`, Phase 5) via Firestore's callback-
    based `on_snapshot` watch, which has no natural async-generator shape
    and is therefore kept out of this persistence-layer contract.
    """

    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client = client or AsyncClient()

    def _incident_ref(self, incident_id: str) -> AsyncDocumentReference:
        return self._client.collection("incidents").document(incident_id)

    async def create_incident(self, state: IncidentState) -> None:
        await self._incident_ref(state.incident_id).set(state.model_dump(mode="json"))

    async def update_incident(self, incident_id: str, **patch: object) -> IncidentState:
        merged = {**patch, "updated_at": datetime.now(UTC)}
        await self._incident_ref(incident_id).update(_JSON_ADAPTER.dump_python(merged, mode="json"))
        return await self.get_incident(incident_id)

    async def get_incident(self, incident_id: str) -> IncidentState:
        snapshot = await self._incident_ref(incident_id).get()
        if not snapshot.exists:
            raise IncidentNotFoundError(incident_id)
        return IncidentState.model_validate(snapshot.to_dict())

    async def list_incidents(self, limit: int = 200) -> list[IncidentState]:
        # Document id ("__name__") is the incident's ULID, which sorts
        # lexicographically in creation order -- ordering by it ascending
        # needs no index (same automatic index Firestore gives every
        # collection, same trick used by list_findings et al. below).
        # Ordering *descending* by "__name__" instead would need a manual
        # composite index (confirmed against a real project: Firestore
        # raises `FailedPrecondition: The query requires an index` for it)
        # -- reversing client-side avoids provisioning one just for this.
        # Fine at demo scale; would need a real descending index (and
        # server-side limit) if the incident collection ever got huge.
        query = self._client.collection("incidents").order_by("__name__")
        incidents = [
            IncidentState.model_validate(snapshot.to_dict()) async for snapshot in query.stream()
        ]
        incidents.reverse()
        return incidents[:limit]

    async def append_step(self, incident_id: str, log: AgentStepLog) -> None:
        ref = self._incident_ref(incident_id).collection("steps").document(log.step_id)
        await ref.set(log.model_dump(mode="json"))

    async def write_finding(self, incident_id: str, finding: DiagnosticFinding) -> None:
        ref = self._incident_ref(incident_id).collection("findings").document(finding.finding_id)
        await ref.set(finding.model_dump(mode="json"))

    async def write_patch(self, incident_id: str, patch: SQLPatchPayload) -> None:
        ref = self._incident_ref(incident_id).collection("patches").document(patch.patch_id)
        await ref.set(patch.model_dump(mode="json"))

    async def get_patch(self, incident_id: str, patch_id: str) -> SQLPatchPayload:
        ref = self._incident_ref(incident_id).collection("patches").document(patch_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            raise PatchNotFoundError(patch_id)
        return SQLPatchPayload.model_validate(snapshot.to_dict())

    async def write_audit(self, incident_id: str, audit: GovernanceAudit) -> None:
        ref = self._incident_ref(incident_id).collection("audits").document(audit.audit_id)
        await ref.set(audit.model_dump(mode="json"))

    async def stream_steps(self, incident_id: str) -> AsyncIterator[AgentStepLog]:
        query = self._incident_ref(incident_id).collection("steps").order_by("step_id")
        async for snapshot in query.stream():
            yield AgentStepLog.model_validate(snapshot.to_dict())

    async def list_findings(self, incident_id: str) -> list[DiagnosticFinding]:
        # ULIDs sort lexicographically by creation time, so ordering by
        # document id ("__name__") gives chronological order for free.
        query = self._incident_ref(incident_id).collection("findings").order_by("__name__")
        return [
            DiagnosticFinding.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def list_patches(self, incident_id: str) -> list[SQLPatchPayload]:
        query = self._incident_ref(incident_id).collection("patches").order_by("__name__")
        return [
            SQLPatchPayload.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def list_audits(self, incident_id: str) -> list[GovernanceAudit]:
        query = self._incident_ref(incident_id).collection("audits").order_by("__name__")
        return [
            GovernanceAudit.model_validate(snapshot.to_dict()) async for snapshot in query.stream()
        ]

    async def list_steps(self, incident_id: str) -> list[AgentStepLog]:
        query = self._incident_ref(incident_id).collection("steps").order_by("step_id")
        return [
            AgentStepLog.model_validate(snapshot.to_dict()) async for snapshot in query.stream()
        ]
