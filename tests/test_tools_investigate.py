from __future__ import annotations

import httpx
import pytest

from app.agents.tools.investigate import (
    GemmaHttpTriageBackend,
    LocalHeuristicTriageBackend,
    get_triage_backend,
    investigate_incident_logs,
)
from app.config import get_settings
from app.models import IncidentState, new_id
from app.persistence import InMemoryStateManager
from app.persistence.factory import get_state_manager


def _incident(raw_event: dict[str, object]) -> IncidentState:
    return IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P2",
        raw_event=raw_event,
        orchestrator_model="gemini-3.1-pro-preview",
    )


async def test_local_heuristic_backend_handles_schema_drift_scenario() -> None:
    backend = LocalHeuristicTriageBackend()
    finding = await backend.triage(
        resource_uri="bq://proj.ds.orders",
        lookback_minutes=60,
        max_log_lines=500,
        raw_event={"scenario": "schema_drift", "table": "orders", "dropped_column": "email"},
    )
    assert finding.drift_type == "SCHEMA_DRIFT"
    assert finding.affected_columns == ["email"]
    assert finding.confidence > 0.5
    assert finding.evidence


async def test_local_heuristic_backend_handles_data_quality_scenario() -> None:
    backend = LocalHeuristicTriageBackend()
    finding = await backend.triage(
        resource_uri="bq://proj.ds.orders",
        lookback_minutes=60,
        max_log_lines=500,
        raw_event={
            "scenario": "data_quality",
            "table": "orders",
            "column": "total",
            "null_rate": 0.42,
        },
    )
    assert finding.drift_type == "DATA_QUALITY"
    assert finding.affected_columns == ["total"]
    assert "42%" in finding.hypothesis


async def test_local_heuristic_backend_handles_broken_job_scenario() -> None:
    backend = LocalHeuristicTriageBackend()
    finding = await backend.triage(
        resource_uri="bq://proj.ds.orders",
        lookback_minutes=60,
        max_log_lines=500,
        raw_event={"scenario": "broken_job", "job_name": "nightly_etl", "error_message": "OOM"},
    )
    assert finding.drift_type == "BROKEN_JOB"
    assert "nightly_etl" in finding.hypothesis


async def test_local_heuristic_backend_handles_slow_copy_scenario() -> None:
    backend = LocalHeuristicTriageBackend()
    finding = await backend.triage(
        resource_uri="bq://proj.ds.orders",
        lookback_minutes=60,
        max_log_lines=500,
        raw_event={
            "scenario": "slow_copy",
            "table": "orders",
            "job_name": "nightly_customer_copy",
            "filter_column": "customer_id",
            "duration_minutes": 47,
            "baseline_minutes": 6,
        },
    )
    assert finding.drift_type == "PERFORMANCE_DEGRADATION"
    assert finding.affected_columns == ["customer_id"]
    assert "nightly_customer_copy" in finding.hypothesis
    assert "clustering it on `customer_id`" in finding.hypothesis
    # Steers the real Gemini patcher away from inventing CREATE INDEX.
    assert "CREATE INDEX" in finding.hypothesis
    assert finding.evidence


async def test_local_heuristic_backend_handles_unknown_scenario() -> None:
    backend = LocalHeuristicTriageBackend()
    finding = await backend.triage(
        resource_uri="bq://proj.ds.orders",
        lookback_minutes=60,
        max_log_lines=500,
        raw_event={},
    )
    assert finding.drift_type == "UNKNOWN"
    assert finding.confidence < 0.5
    assert finding.evidence == []


async def test_gemma_backend_parses_response_via_injected_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/triage"
        return httpx.Response(
            200,
            json={
                "hypothesis": "Something broke",
                "drift_type": "SCHEMA_DRIFT",
                "affected_columns": ["email"],
                "confidence": 0.9,
                "evidence": [
                    {
                        "source": "cloud_logging",
                        "log_line": "ALTER TABLE t DROP COLUMN email",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "severity": "ERROR",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        backend = GemmaHttpTriageBackend("http://gemma.local", client=client)
        finding = await backend.triage(
            resource_uri="bq://proj.ds.t",
            lookback_minutes=60,
            max_log_lines=500,
            raw_event={},
        )

    assert finding.drift_type == "SCHEMA_DRIFT"
    assert finding.triage_model == "gemma-2-9b-it"
    assert finding.evidence[0].severity == "ERROR"


def test_get_triage_backend_defaults_to_local_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WARDEN_GEMMA_ENDPOINT", raising=False)
    get_settings.cache_clear()
    try:
        backend = get_triage_backend()
        assert isinstance(backend, LocalHeuristicTriageBackend)
    finally:
        get_settings.cache_clear()


def test_get_triage_backend_uses_gemma_when_endpoint_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_GEMMA_ENDPOINT", "http://gemma.local")
    get_settings.cache_clear()
    try:
        backend = get_triage_backend()
        assert isinstance(backend, GemmaHttpTriageBackend)
    finally:
        get_settings.cache_clear()


async def test_investigate_incident_logs_tool_persists_finding_and_returns_envelope() -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)

    incident = _incident(
        {"scenario": "data_quality", "table": "orders", "column": "total", "null_rate": 0.42}
    )
    await state_manager.create_incident(incident)

    result = await investigate_incident_logs(
        incident_id=incident.incident_id,
        resource_uri=incident.resource_uri,
    )

    assert result["finding_id"]
    assert result["drift_type"] == "DATA_QUALITY"
    expected_path = f"incidents/{incident.incident_id}/findings/{result['finding_id']}"
    assert result["firestore_path"] == expected_path

    findings = await state_manager.list_findings(incident.incident_id)
    assert len(findings) == 1
    assert findings[0].finding_id == result["finding_id"]
