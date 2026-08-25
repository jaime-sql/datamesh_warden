"""Tests for the FastAPI HTTP surface (`app/api/*`).

Exercised entirely against `InMemoryStateManager` over ASGI (no real
server, no network, no GCP). The `/events/ingest` endpoint's background
orchestrator run is stubbed out via `get_orchestrator`'s dependency
override -- exactly the same seam `tests/test_orchestrator_loop.py` uses at
the orchestrator level -- so these tests stay fast and deterministic
without needing Gemini credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.api.deps import drain_background_tasks, get_orchestrator
from app.api.main import app
from app.models import GovernanceAudit, IncidentState, SQLPatchPayload, new_id
from app.models.enums import GovernanceVerdict, IncidentStatus
from app.persistence import InMemoryStateManager, StateManager
from app.persistence.factory import get_state_manager


class _StubOrchestrator:
    """Replaces `WardenOrchestrator` for ingest tests: no Gemini calls, just
    a deterministic status transition so we can assert the background task
    actually ran."""

    def __init__(self, state_manager: StateManager, final_status: IncidentStatus) -> None:
        self._state_manager = state_manager
        self._final_status = final_status

    async def run(self, incident_id: str) -> None:
        await self._state_manager.update_incident(
            incident_id, status=self._final_status, summary="stub run"
        )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.pop(get_orchestrator, None)


async def _seed_awaiting_approval_incident(
    state_manager: StateManager, *, verdict: GovernanceVerdict = "PASS"
) -> tuple[IncidentState, SQLPatchPayload, GovernanceAudit]:
    incident = IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event={},
        orchestrator_model="gemini-3.1-pro-preview",
        status="AWAITING_APPROVAL",
    )
    await state_manager.create_incident(incident)

    patch = SQLPatchPayload(
        patch_id=new_id(),
        linked_finding_id=new_id(),
        patch_kind="DDL",
        sandbox_sql="ALTER TABLE sandbox.t ADD COLUMN email STRING",
        production_sql="ALTER TABLE prod.t ADD COLUMN email STRING",
        validation_status="SANDBOX_PASS",
        patcher_model="test-fixture",
    )
    await state_manager.write_patch(incident.incident_id, patch)

    audit = GovernanceAudit(
        audit_id=new_id(),
        linked_patch_id=patch.patch_id,
        verdict=verdict,
        rationale="test fixture",
    )
    await state_manager.write_audit(incident.incident_id, audit)

    return incident, patch, audit


async def test_healthz_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ingest_returns_202_and_background_run_completes(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)

    app.dependency_overrides[get_orchestrator] = lambda: _StubOrchestrator(
        state_manager, "AWAITING_APPROVAL"
    )

    response = await client.post(
        "/events/ingest",
        json={
            "source": "synthetic_probe",
            "resource_uri": "bq://proj.ds.orders",
            "severity": "P1",
            "raw_event": {"scenario": "schema_drift"},
        },
    )
    assert response.status_code == 202
    body = response.json()
    incident_id = body["incident_id"]
    assert body["status"] == "INGESTED"

    await drain_background_tasks()

    final = await state_manager.get_incident(incident_id)
    assert final.status == "AWAITING_APPROVAL"
    assert final.summary == "stub run"


async def test_ingest_rejects_invalid_payload(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/events/ingest",
        json={"source": "not_a_real_source", "resource_uri": "bq://proj.ds.orders"},
    )
    assert response.status_code == 422


async def test_get_incident_returns_state(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, _patch, _audit = await _seed_awaiting_approval_incident(state_manager)

    response = await client.get(f"/incidents/{incident.incident_id}")
    assert response.status_code == 200
    assert response.json()["incident_id"] == incident.incident_id


async def test_get_incident_404_for_unknown_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/incidents/does-not-exist")
    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["steps", "findings", "patches", "audits"])
async def test_list_subresources_404_for_unknown_incident(
    client: httpx.AsyncClient, suffix: str
) -> None:
    response = await client.get(f"/incidents/does-not-exist/{suffix}")
    assert response.status_code == 404


async def test_list_patches_and_audits_return_seeded_data(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, patch, audit = await _seed_awaiting_approval_incident(state_manager)

    patches_response = await client.get(f"/incidents/{incident.incident_id}/patches")
    assert patches_response.status_code == 200
    assert [p["patch_id"] for p in patches_response.json()] == [patch.patch_id]

    audits_response = await client.get(f"/incidents/{incident.incident_id}/audits")
    assert audits_response.status_code == 200
    assert [a["audit_id"] for a in audits_response.json()] == [audit.audit_id]

    findings_response = await client.get(f"/incidents/{incident.incident_id}/findings")
    assert findings_response.status_code == 200
    assert findings_response.json() == []


async def test_execute_happy_path_resolves_incident(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, patch, _audit = await _seed_awaiting_approval_incident(state_manager)

    response = await client.post(f"/incidents/{incident.incident_id}/execute", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"

    steps = await state_manager.list_steps(incident.incident_id)
    execution_steps = [s for s in steps if s.kind == "EXECUTION"]
    assert len(execution_steps) == 1
    assert execution_steps[0].status == "OK"
    expected_ref = f"incidents/{incident.incident_id}/patches/{patch.patch_id}"
    assert execution_steps[0].tool_result_ref == expected_ref


async def test_execute_without_body_uses_latest_patch(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, _patch, _audit = await _seed_awaiting_approval_incident(state_manager)

    response = await client.post(f"/incidents/{incident.incident_id}/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


async def test_execute_conflicts_when_not_awaiting_approval(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident = IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event={},
        orchestrator_model="gemini-3.1-pro-preview",
        status="DIAGNOSING",
    )
    await state_manager.create_incident(incident)

    response = await client.post(f"/incidents/{incident.incident_id}/execute", json={})
    assert response.status_code == 409


async def test_execute_conflicts_when_governance_blocked(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, _patch, _audit = await _seed_awaiting_approval_incident(
        state_manager, verdict="BLOCK"
    )

    response = await client.post(f"/incidents/{incident.incident_id}/execute", json={})
    assert response.status_code == 409

    final = await state_manager.get_incident(incident.incident_id)
    assert final.status == "AWAITING_APPROVAL"  # execution never happened


async def test_reject_happy_path(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident, _patch, _audit = await _seed_awaiting_approval_incident(state_manager)

    response = await client.post(
        f"/incidents/{incident.incident_id}/reject", json={"reason": "too risky"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"

    steps = await state_manager.list_steps(incident.incident_id)
    decision_steps = [s for s in steps if s.kind == "USER_DECISION"]
    assert len(decision_steps) == 1
    assert decision_steps[0].content_markdown == "too risky"


async def test_reject_conflicts_when_not_awaiting_approval(client: httpx.AsyncClient) -> None:
    state_manager = get_state_manager()
    incident = IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event={},
        orchestrator_model="gemini-3.1-pro-preview",
        status="RESOLVED",
    )
    await state_manager.create_incident(incident)

    response = await client.post(f"/incidents/{incident.incident_id}/reject")
    assert response.status_code == 409
