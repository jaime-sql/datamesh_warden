from __future__ import annotations

import asyncio

import pytest

from app.models import AgentStepLog, IncidentState, new_id
from app.persistence import IncidentNotFoundError, InMemoryStateManager, format_step_id


def _new_incident() -> IncidentState:
    return IncidentState(
        incident_id=new_id(),
        source="synthetic_probe",
        resource_uri="bq://proj.ds.table",
        severity="P1",
        raw_event={"reason": "schema_drift"},
        orchestrator_model="gemini-3.1-pro-preview",
    )


async def test_create_and_get_incident() -> None:
    manager = InMemoryStateManager()
    incident = _new_incident()
    await manager.create_incident(incident)

    fetched = await manager.get_incident(incident.incident_id)
    assert fetched == incident


async def test_get_missing_incident_raises() -> None:
    manager = InMemoryStateManager()
    with pytest.raises(IncidentNotFoundError):
        await manager.get_incident("does-not-exist")


async def test_update_incident_persists_patch_and_bumps_updated_at() -> None:
    manager = InMemoryStateManager()
    incident = _new_incident()
    await manager.create_incident(incident)

    updated = await manager.update_incident(
        incident.incident_id, status="DIAGNOSING", turn_count=1
    )

    assert updated.status == "DIAGNOSING"
    assert updated.turn_count == 1
    assert updated.updated_at >= incident.created_at

    refetched = await manager.get_incident(incident.incident_id)
    assert refetched.status == "DIAGNOSING"


async def test_append_step_requires_existing_incident() -> None:
    manager = InMemoryStateManager()
    orphan_step = AgentStepLog(
        step_id=format_step_id(1),
        parent_incident_id="missing",
        kind="MODEL_TURN",
        actor="orchestrator",
        status="OK",
    )
    with pytest.raises(IncidentNotFoundError):
        await manager.append_step("missing", orphan_step)


async def test_stream_steps_replays_history_then_delivers_live_updates() -> None:
    manager = InMemoryStateManager()
    incident = _new_incident()
    await manager.create_incident(incident)

    first_step = AgentStepLog(
        step_id=format_step_id(1),
        parent_incident_id=incident.incident_id,
        kind="MODEL_TURN",
        actor="orchestrator",
        status="OK",
    )
    await manager.append_step(incident.incident_id, first_step)

    received: list[AgentStepLog] = []

    async def _consume() -> None:
        async for step in manager.stream_steps(incident.incident_id):
            received.append(step)
            if len(received) == 2:
                break

    consume_task = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)  # let the subscriber register and replay history

    second_step = AgentStepLog(
        step_id=format_step_id(2),
        parent_incident_id=incident.incident_id,
        kind="TOOL_CALL",
        actor="sub_agent_1_gemma",
        tool_name="investigate_incident_logs",
        status="OK",
    )
    await manager.append_step(incident.incident_id, second_step)

    await asyncio.wait_for(consume_task, timeout=2.0)

    assert [s.step_id for s in received] == ["0001", "0002"]


async def test_write_finding_patch_audit_and_list_helpers() -> None:
    from app.models import DiagnosticFinding, GovernanceAudit, SQLPatchPayload

    manager = InMemoryStateManager()
    incident = _new_incident()
    await manager.create_incident(incident)

    finding = DiagnosticFinding(
        finding_id=new_id(),
        hypothesis="Column dropped upstream",
        drift_type="SCHEMA_DRIFT",
        confidence=0.9,
        triage_model="gemma-2-9b-it",
    )
    await manager.write_finding(incident.incident_id, finding)

    patch = SQLPatchPayload(
        patch_id=new_id(),
        linked_finding_id=finding.finding_id,
        patch_kind="DDL",
        sandbox_sql="ALTER TABLE sandbox.t ADD COLUMN email STRING",
        production_sql="ALTER TABLE prod.t ADD COLUMN email STRING",
        validation_status="SANDBOX_PASS",
        patcher_model="gemini-2.5-flash",
    )
    await manager.write_patch(incident.incident_id, patch)

    audit = GovernanceAudit(
        audit_id=new_id(),
        linked_patch_id=patch.patch_id,
        verdict="PASS",
        rationale="No PII columns touched.",
    )
    await manager.write_audit(incident.incident_id, audit)

    assert manager.list_findings(incident.incident_id) == [finding]
    assert manager.list_patches(incident.incident_id) == [patch]
    assert manager.list_audits(incident.incident_id) == [audit]
