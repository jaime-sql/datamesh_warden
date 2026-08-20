from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models import (
    AgentStepLog,
    ColumnSpec,
    DiagnosticFinding,
    EvidenceItem,
    GovernanceAudit,
    IncidentState,
    PolicyCheck,
    SQLPatchPayload,
    new_id,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _base_incident_kwargs() -> dict[str, object]:
    return {
        "incident_id": new_id(),
        "source": "manual_demo",
        "resource_uri": "bq://proj.ds.table",
        "severity": "P2",
        "raw_event": {},
        "orchestrator_model": "gemini-3.1-pro-preview",
    }


def test_incident_state_round_trip() -> None:
    incident = IncidentState(
        incident_id=new_id(),
        source="synthetic_probe",
        resource_uri="bq://proj.ds.table",
        severity="P1",
        raw_event={"reason": "schema_drift"},
        orchestrator_model="gemini-3.1-pro-preview",
    )

    dumped = incident.model_dump(mode="json")
    restored = IncidentState.model_validate(dumped)

    assert restored == incident
    assert restored.status == "INGESTED"
    assert restored.turn_count == 0


def test_incident_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        IncidentState.model_validate({**_base_incident_kwargs(), "not_a_real_field": True})


def test_incident_state_rejects_invalid_status_literal() -> None:
    with pytest.raises(ValidationError):
        IncidentState.model_validate({**_base_incident_kwargs(), "status": "NOT_A_REAL_STATUS"})


def test_agent_step_log_step_id_zero_padding_sorts_lexicographically() -> None:
    steps = [
        AgentStepLog(
            step_id=f"{i:04d}",
            parent_incident_id="inc-1",
            kind="TOOL_CALL",
            actor="orchestrator",
            status="OK",
        )
        for i in (3, 1, 2, 10)
    ]
    ordered = sorted(steps, key=lambda s: s.step_id)
    assert [s.step_id for s in ordered] == ["0001", "0002", "0003", "0010"]


def test_diagnostic_finding_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        DiagnosticFinding(
            finding_id=new_id(),
            hypothesis="x",
            drift_type="SCHEMA_DRIFT",
            confidence=1.5,
            triage_model="gemma-2-9b-it",
        )

    finding = DiagnosticFinding(
        finding_id=new_id(),
        hypothesis="Column dropped upstream",
        evidence=[
            EvidenceItem(
                source="cloud_logging",
                log_line="ALTER TABLE t DROP COLUMN email",
                timestamp=_now(),
                severity="ERROR",
            )
        ],
        drift_type="SCHEMA_DRIFT",
        affected_columns=["email"],
        confidence=0.86,
        triage_model="gemma-2-9b-it",
    )
    assert finding.evidence[0].severity == "ERROR"
    assert finding.affected_columns == ["email"]


def test_sql_patch_payload_schema_diff_fields() -> None:
    patch = SQLPatchPayload(
        patch_id=new_id(),
        linked_finding_id=new_id(),
        patch_kind="DDL",
        sandbox_sql="ALTER TABLE sandbox.t ADD COLUMN email STRING",
        production_sql="ALTER TABLE prod.t ADD COLUMN email STRING",
        before_schema=[ColumnSpec(name="id", type="INT64", mode="REQUIRED")],
        after_schema=[
            ColumnSpec(name="id", type="INT64", mode="REQUIRED"),
            ColumnSpec(name="email", type="STRING", mode="NULLABLE"),
        ],
        validation_status="SANDBOX_PASS",
        patcher_model="gemini-2.5-flash",
    )
    assert len(patch.after_schema) == len(patch.before_schema) + 1


def test_governance_audit_defaults_require_approval() -> None:
    audit = GovernanceAudit(
        audit_id=new_id(),
        linked_patch_id=new_id(),
        verdict="PASS",
        policy_checks=[
            PolicyCheck(
                policy_id="pii-drop-guard",
                description="Blocks DROP COLUMN on PII-tagged tables",
                result="PASS",
                detail="No PII columns touched",
            )
        ],
        rationale="No PII columns are affected.",
    )
    assert audit.requires_human_approval is True
    assert audit.verdict == "PASS"
