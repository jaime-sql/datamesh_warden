"""`RemediationExecutor` -- runs an *approved* patch for real.

See docs/architecture.md section 1 ("RemediationExecutor"). This is the
code path reached once a human clicks Approve in the Streamlit war room
(Phase 5) via `POST /incidents/{id}/execute` (Phase 4's `app/api/routes.py`).

Deliberately paranoid: it never trusts client-supplied state beyond an
optional `patch_id`. It reloads the incident, the patch, and the most
recent governance audit for that patch straight from the `StateManager`,
and refuses to run unless the incident is `AWAITING_APPROVAL` and the
governance verdict isn't `BLOCK` (the orchestrator's own loop already
guarantees blocked patches never reach `AWAITING_APPROVAL` in the first
place -- this is a defense-in-depth check against stale or tampered
client state, not a normal-path check).

Two backends, mirroring the local/cloud pattern used throughout Phase 2:
`LocalHeuristicExecutorBackend` (no BigQuery access, simulates a
successful run) and `BigQueryExecutorBackend` (runs `production_sql` for
real via `app.agents.bq_sandbox.run_sandbox_statement`, only selected once
`Settings.warden_mode == "cloud"`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from app.agents.bq_sandbox import get_bigquery_client, run_sandbox_statement
from app.config import get_settings
from app.models.state import AgentStepLog, GovernanceAudit, IncidentState, SQLPatchPayload
from app.persistence.base import StateManager, format_step_id
from app.persistence.factory import get_state_manager

_LOCAL_EXECUTION_LATENCY_MS = 5


class IncidentNotAwaitingApprovalError(RuntimeError):
    """Raised when execute/reject is attempted on an incident that isn't
    (or is no longer) `AWAITING_APPROVAL`."""


class NoPatchAvailableError(RuntimeError):
    """Raised when an incident has no `SQLPatchPayload` to execute."""


class NoGovernanceAuditError(RuntimeError):
    """Raised when the target patch has never been through a governance
    check -- execution must never proceed without one."""


class GovernanceBlockError(RuntimeError):
    """Raised when the most recent governance audit for the patch is a
    `BLOCK` verdict."""


class ExecutorBackend(Protocol):
    async def execute(self, production_sql: str) -> tuple[int, int]:
        """Run `production_sql`. Returns `(latency_ms, affected_row_count)`."""
        ...


class LocalHeuristicExecutorBackend:
    """Zero-resource fallback: simulates a successful, instantaneous run so
    the demo flow works end to end without any GCP project."""

    async def execute(self, production_sql: str) -> tuple[int, int]:
        return (_LOCAL_EXECUTION_LATENCY_MS, 0)


class BigQueryExecutorBackend:
    """Real implementation: executes `production_sql` against BigQuery.
    Requires `WARDEN_MODE=cloud` and a real GCP project."""

    async def execute(self, production_sql: str) -> tuple[int, int]:
        client = get_bigquery_client()
        return await run_sandbox_statement(client, production_sql)


def get_executor_backend() -> ExecutorBackend:
    settings = get_settings()
    if settings.warden_mode == "cloud":
        return BigQueryExecutorBackend()
    return LocalHeuristicExecutorBackend()


async def _latest_audit_for_patch(
    state_manager: StateManager, incident_id: str, patch_id: str
) -> GovernanceAudit:
    audits = await state_manager.list_audits(incident_id)
    matching = [audit for audit in audits if audit.linked_patch_id == patch_id]
    if not matching:
        raise NoGovernanceAuditError(
            f"patch {patch_id} on incident {incident_id} has no governance audit"
        )
    return matching[-1]


async def _next_step_sequence(state_manager: StateManager, incident_id: str) -> int:
    existing = await state_manager.list_steps(incident_id)
    return len(existing) + 1


async def _require_awaiting_approval(
    state_manager: StateManager, incident_id: str
) -> IncidentState:
    incident = await state_manager.get_incident(incident_id)
    if incident.status != "AWAITING_APPROVAL":
        raise IncidentNotAwaitingApprovalError(
            f"incident {incident_id} is {incident.status!r}, not AWAITING_APPROVAL"
        )
    return incident


async def execute_incident(
    incident_id: str,
    *,
    patch_id: str | None = None,
    state_manager: StateManager | None = None,
    backend: ExecutorBackend | None = None,
) -> IncidentState:
    """Human approved the incident: validate governance one more time, run
    the patch's `production_sql` for real, log an `EXECUTION` step, and
    mark the incident `RESOLVED`. On any failure the incident is marked
    `FAILED` (with `error` set) rather than raising, except for the
    pre-flight validation errors above, which the API layer maps to 409s.
    """
    sm = state_manager or get_state_manager()
    await _require_awaiting_approval(sm, incident_id)

    if patch_id is None:
        patches = await sm.list_patches(incident_id)
        if not patches:
            raise NoPatchAvailableError(f"incident {incident_id} has no patch to execute")
        patch_id = patches[-1].patch_id

    patch: SQLPatchPayload = await sm.get_patch(incident_id, patch_id)
    audit = await _latest_audit_for_patch(sm, incident_id, patch_id)
    if audit.verdict == "BLOCK":
        raise GovernanceBlockError(
            f"governance audit {audit.audit_id} for patch {patch_id} is BLOCK"
        )

    step_sequence = await _next_step_sequence(sm, incident_id)
    approval_now = datetime.now(UTC)
    await sm.append_step(
        incident_id,
        AgentStepLog(
            step_id=format_step_id(step_sequence),
            parent_incident_id=incident_id,
            kind="USER_DECISION",
            actor="human",
            content_markdown=(
                f"Approved execution of patch `{patch_id}` "
                f"(governance audit `{audit.audit_id}`, verdict {audit.verdict})."
            ),
            status="OK",
            started_at=approval_now,
            finished_at=approval_now,
        ),
    )

    exec_backend = backend or get_executor_backend()
    exec_step_sequence = step_sequence + 1
    exec_started_at = datetime.now(UTC)
    try:
        latency_ms, affected_rows = await exec_backend.execute(patch.production_sql)
    except Exception as exc:  # noqa: BLE001 - a failed execution must not crash the request
        await sm.append_step(
            incident_id,
            AgentStepLog(
                step_id=format_step_id(exec_step_sequence),
                parent_incident_id=incident_id,
                kind="EXECUTION",
                actor="executor",
                tool_result_ref=f"incidents/{incident_id}/patches/{patch_id}",
                status="ERROR",
                error_detail=str(exc),
                started_at=exec_started_at,
                finished_at=datetime.now(UTC),
            ),
        )
        return await sm.update_incident(
            incident_id, status="FAILED", error=f"execution_failed: {exc}"
        )

    await sm.append_step(
        incident_id,
        AgentStepLog(
            step_id=format_step_id(exec_step_sequence),
            parent_incident_id=incident_id,
            kind="EXECUTION",
            actor="executor",
            tool_result_ref=f"incidents/{incident_id}/patches/{patch_id}",
            content_markdown=f"Executed patch `{patch_id}` ({affected_rows} rows affected).",
            latency_ms=latency_ms,
            status="OK",
            started_at=exec_started_at,
            finished_at=datetime.now(UTC),
        ),
    )
    return await sm.update_incident(incident_id, status="RESOLVED")


async def reject_incident(
    incident_id: str,
    *,
    reason: str | None = None,
    state_manager: StateManager | None = None,
) -> IncidentState:
    """Human rejected the incident's proposed remediation."""
    sm = state_manager or get_state_manager()
    await _require_awaiting_approval(sm, incident_id)

    step_sequence = await _next_step_sequence(sm, incident_id)
    now = datetime.now(UTC)
    await sm.append_step(
        incident_id,
        AgentStepLog(
            step_id=format_step_id(step_sequence),
            parent_incident_id=incident_id,
            kind="USER_DECISION",
            actor="human",
            content_markdown=reason or "Rejected by human reviewer.",
            status="OK",
            started_at=now,
            finished_at=now,
        ),
    )
    return await sm.update_incident(incident_id, status="REJECTED")
