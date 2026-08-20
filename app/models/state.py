"""Pydantic v2 models persisted to Firestore (see docs/architecture.md §2).

Every model forbids extra fields so a typo in a tool's returned dict or a
stray key from a model response fails fast at validation time rather than
silently disappearing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    ColumnMode,
    DriftType,
    EvidenceSeverity,
    GovernanceVerdict,
    IncidentSource,
    IncidentStatus,
    PatchKind,
    PolicyCheckResult,
    Severity,
    StepActor,
    StepKind,
    StepStatus,
    ValidationStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WardenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvidenceItem(WardenModel):
    source: str
    log_line: str
    timestamp: datetime
    severity: EvidenceSeverity


class ColumnSpec(WardenModel):
    name: str
    type: str
    mode: ColumnMode
    description: str | None = None


class PolicyCheck(WardenModel):
    policy_id: str
    description: str
    result: PolicyCheckResult
    detail: str


class IncidentState(WardenModel):
    """Root document at `incidents/{incident_id}`."""

    incident_id: str
    source: IncidentSource
    resource_uri: str
    severity: Severity
    raw_event: dict[str, Any]
    status: IncidentStatus = "INGESTED"
    active_step_id: str | None = None
    summary: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    orchestrator_model: str
    turn_count: int = 0
    error: str | None = None


class AgentStepLog(WardenModel):
    """One entry in the append-only `incidents/{id}/steps` subcollection."""

    step_id: str
    parent_incident_id: str
    kind: StepKind
    actor: StepActor
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result_ref: str | None = None
    content_markdown: str | None = None
    latency_ms: int | None = None
    status: StepStatus = "RUNNING"
    error_detail: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None


class DiagnosticFinding(WardenModel):
    """Output of Sub-Agent 1 (`investigate_incident_logs`)."""

    finding_id: str
    hypothesis: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    drift_type: DriftType
    affected_columns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    triage_model: str


class SQLPatchPayload(WardenModel):
    """Output of Sub-Agent 2 (`generate_and_test_patch`)."""

    patch_id: str
    linked_finding_id: str
    patch_kind: PatchKind
    sandbox_sql: str
    production_sql: str
    before_schema: list[ColumnSpec] = Field(default_factory=list)
    after_schema: list[ColumnSpec] = Field(default_factory=list)
    dry_run_bytes_processed: int = 0
    sandbox_execution_ms: int = 0
    sandbox_row_delta: int = 0
    validation_status: ValidationStatus
    validation_errors: list[str] = Field(default_factory=list)
    patcher_model: str


class GovernanceAudit(WardenModel):
    """Output of Sub-Agent 3 (`verify_governance_policy`)."""

    audit_id: str
    linked_patch_id: str
    verdict: GovernanceVerdict
    policy_checks: list[PolicyCheck] = Field(default_factory=list)
    pii_columns_touched: list[str] = Field(default_factory=list)
    requires_human_approval: bool = True
    rationale: str
