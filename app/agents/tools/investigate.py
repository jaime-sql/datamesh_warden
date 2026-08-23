"""Sub-Agent 1: log triage -> DiagnosticFinding.

`LocalHeuristicTriageBackend` needs no external resources: it derives a
plausible finding purely from the incident's `raw_event` payload, using the
same canned `scenario` convention the Streamlit demo presets will use in
Phase 5 (`scenario` one of "schema_drift" | "data_quality" | "broken_job",
plus scenario-specific fields such as `table`, `dropped_column`, `column`,
`null_rate`, `job_name`, `error_message`).

`GemmaHttpTriageBackend` is the real implementation and is only selected
once `WARDEN_GEMMA_ENDPOINT` is configured -- i.e. once a Gemma-on-Cloud-Run
triage service has actually been deployed, which we have not done yet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.models.ids import new_id
from app.models.state import DiagnosticFinding, EvidenceItem
from app.persistence.factory import get_state_manager

_GEMMA_TRIAGE_MODEL = "gemma-2-9b-it"
_LOCAL_TRIAGE_MODEL = "local-heuristic-v1"
_TRIAGE_TIMEOUT_S = 30


def _now() -> datetime:
    return datetime.now(UTC)


class LogTriageBackend(Protocol):
    async def triage(
        self,
        resource_uri: str,
        lookback_minutes: int,
        max_log_lines: int,
        raw_event: dict[str, Any],
    ) -> DiagnosticFinding: ...


class LocalHeuristicTriageBackend:
    """Zero-resource fallback. Deterministic given the same `raw_event`."""

    async def triage(
        self,
        resource_uri: str,
        lookback_minutes: int,
        max_log_lines: int,
        raw_event: dict[str, Any],
    ) -> DiagnosticFinding:
        scenario = raw_event.get("scenario")

        if scenario == "schema_drift":
            return self._schema_drift_finding(raw_event)
        if scenario == "data_quality":
            return self._data_quality_finding(raw_event)
        if scenario == "broken_job":
            return self._broken_job_finding(raw_event)
        return self._unknown_finding()

    def _schema_drift_finding(self, raw_event: dict[str, Any]) -> DiagnosticFinding:
        table = raw_event.get("table", "the target table")
        column = raw_event.get("dropped_column", "unknown_column")
        return DiagnosticFinding(
            finding_id=new_id(),
            hypothesis=(
                f"Column `{column}` appears to have been dropped from `{table}` by an upstream job."
            ),
            evidence=[
                EvidenceItem(
                    source="synthetic_scenario",
                    log_line=f"ALTER TABLE {table} DROP COLUMN {column}",
                    timestamp=_now(),
                    severity="ERROR",
                )
            ],
            drift_type="SCHEMA_DRIFT",
            affected_columns=[column],
            confidence=0.82,
            triage_model=_LOCAL_TRIAGE_MODEL,
        )

    def _data_quality_finding(self, raw_event: dict[str, Any]) -> DiagnosticFinding:
        table = raw_event.get("table", "the target table")
        column = raw_event.get("column", "unknown_column")
        null_rate = float(raw_event.get("null_rate", 0.0))
        return DiagnosticFinding(
            finding_id=new_id(),
            hypothesis=(
                f"Column `{column}` on `{table}` has an abnormal null rate of {null_rate:.0%}."
            ),
            evidence=[
                EvidenceItem(
                    source="synthetic_scenario",
                    log_line=(
                        f"data_quality_check(table={table}, column={column}, null_rate={null_rate})"
                    ),
                    timestamp=_now(),
                    severity="WARN",
                )
            ],
            drift_type="DATA_QUALITY",
            affected_columns=[column],
            confidence=0.75,
            triage_model=_LOCAL_TRIAGE_MODEL,
        )

    def _broken_job_finding(self, raw_event: dict[str, Any]) -> DiagnosticFinding:
        job_name = raw_event.get("job_name", "unknown_job")
        error_message = raw_event.get("error_message", "unspecified failure")
        return DiagnosticFinding(
            finding_id=new_id(),
            hypothesis=f"Pipeline job `{job_name}` failed: {error_message}",
            evidence=[
                EvidenceItem(
                    source="synthetic_scenario",
                    log_line=f"job={job_name} status=FAILED error={error_message}",
                    timestamp=_now(),
                    severity="ERROR",
                )
            ],
            drift_type="BROKEN_JOB",
            affected_columns=[],
            confidence=0.7,
            triage_model=_LOCAL_TRIAGE_MODEL,
        )

    def _unknown_finding(self) -> DiagnosticFinding:
        return DiagnosticFinding(
            finding_id=new_id(),
            hypothesis="Insufficient signal to determine a root cause from the provided context.",
            evidence=[],
            drift_type="UNKNOWN",
            affected_columns=[],
            confidence=0.3,
            triage_model=_LOCAL_TRIAGE_MODEL,
        )


class GemmaHttpTriageBackend:
    """Calls a Gemma-on-Cloud-Run triage endpoint.

    Contract: POST {endpoint}/triage with JSON {resource_uri,
    lookback_minutes, max_log_lines, raw_event}; expects a JSON response
    with {hypothesis, drift_type, affected_columns, confidence, evidence}.
    """

    def __init__(self, endpoint: str, client: httpx.AsyncClient | None = None) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._client = client

    async def triage(
        self,
        resource_uri: str,
        lookback_minutes: int,
        max_log_lines: int,
        raw_event: dict[str, Any],
    ) -> DiagnosticFinding:
        payload = {
            "resource_uri": resource_uri,
            "lookback_minutes": lookback_minutes,
            "max_log_lines": max_log_lines,
            "raw_event": raw_event,
        }
        client = self._client or httpx.AsyncClient(timeout=_TRIAGE_TIMEOUT_S)
        should_close = self._client is None
        try:
            response = await client.post(f"{self._endpoint}/triage", json=payload)
            response.raise_for_status()
            data = response.json()
        finally:
            if should_close:
                await client.aclose()

        return DiagnosticFinding(
            finding_id=new_id(),
            hypothesis=data["hypothesis"],
            evidence=[EvidenceItem.model_validate(item) for item in data.get("evidence", [])],
            drift_type=data["drift_type"],
            affected_columns=data.get("affected_columns", []),
            confidence=data["confidence"],
            triage_model=_GEMMA_TRIAGE_MODEL,
        )


def get_triage_backend() -> LogTriageBackend:
    settings = get_settings()
    if settings.warden_gemma_endpoint:
        return GemmaHttpTriageBackend(settings.warden_gemma_endpoint)
    return LocalHeuristicTriageBackend()


async def investigate_incident_logs(
    incident_id: str,
    resource_uri: str,
    lookback_minutes: int = 60,
    max_log_lines: int = 500,
) -> dict[str, Any]:
    """Triage raw Cloud Logging / BigQuery INFORMATION_SCHEMA entries for the
    referenced resource and produce a structured root-cause hypothesis.

    Delegates to a Gemma-2 model hosted on a separate Cloud Run service
    (URL from env WARDEN_GEMMA_ENDPOINT), or to a local heuristic fallback
    when that endpoint isn't configured. Gemma is cheap and good at
    high-volume log summarization; the orchestrator (Gemini 3 Pro) then
    reasons over the compact JSON returned here.

    Args:
        incident_id: Firestore incident doc id. Used to persist the finding.
        resource_uri: Fully-qualified resource, e.g. bq://proj.ds.table.
        lookback_minutes: How far back to pull logs. 5..1440.
        max_log_lines: Cap on lines sent to Gemma to control latency.

    Returns:
        A dict with finding_id, hypothesis, drift_type, affected_columns,
        confidence, evidence_count, and firestore_path.
    """
    state_manager = get_state_manager()
    incident = await state_manager.get_incident(incident_id)
    backend = get_triage_backend()

    try:
        finding = await asyncio.wait_for(
            backend.triage(
                resource_uri=resource_uri,
                lookback_minutes=lookback_minutes,
                max_log_lines=max_log_lines,
                raw_event=incident.raw_event,
            ),
            timeout=_TRIAGE_TIMEOUT_S,
        )
    except TimeoutError:
        return {"error": "TIMEOUT", "finding_id": None}

    await state_manager.write_finding(incident_id, finding)

    return {
        "finding_id": finding.finding_id,
        "hypothesis": finding.hypothesis,
        "drift_type": finding.drift_type,
        "affected_columns": finding.affected_columns,
        "confidence": finding.confidence,
        "evidence_count": len(finding.evidence),
        "firestore_path": f"incidents/{incident_id}/findings/{finding.finding_id}",
    }
