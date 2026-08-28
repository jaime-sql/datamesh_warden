"""HTTP surface for DataMesh Warden (see docs/architecture.md section 1).

Two logical route groups live in this one module, since the resource model
is small: event ingestion (kicks off the async orchestrator loop, returns
202 without blocking on reasoning) and incident read/decision endpoints
(reads for the Streamlit war room, plus the human-in-the-loop
execute/reject actions).

Domain exceptions (`IncidentNotFoundError`, `GovernanceBlockError`, etc.)
are deliberately left unhandled here -- they propagate to the exception
handlers registered in `app/api/main.py`, which map them to the right HTTP
status codes. This keeps every route body focused on the happy path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.agents.executor import execute_incident, reject_incident
from app.agents.orchestrator import WardenOrchestrator
from app.agents.pipeline_health import check_pipeline_health
from app.api.deps import get_orchestrator, run_in_background
from app.api.schemas import (
    ExecuteRequest,
    IncidentIngestRequest,
    IncidentIngestResponse,
    PipelineHealthResponse,
    RejectRequest,
)
from app.config import get_settings
from app.models.ids import new_id
from app.models.state import (
    AgentStepLog,
    DiagnosticFinding,
    GovernanceAudit,
    IncidentState,
    SQLPatchPayload,
)
from app.persistence.base import StateManager
from app.persistence.factory import get_state_manager

router = APIRouter()


@router.post(
    "/events/ingest",
    response_model=IncidentIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_event(
    payload: IncidentIngestRequest,
    state_manager: StateManager = Depends(get_state_manager),
    orchestrator: WardenOrchestrator = Depends(get_orchestrator),
) -> IncidentIngestResponse:
    incident = IncidentState(
        incident_id=new_id(),
        source=payload.source,
        resource_uri=payload.resource_uri,
        severity=payload.severity,
        raw_event=payload.raw_event,
        orchestrator_model=get_settings().warden_orchestrator_model,
    )
    await state_manager.create_incident(incident)

    # Fire-and-forget: reasoning runs in the background so this endpoint
    # returns immediately (see the ingest-latency budget in
    # docs/architecture.md section 4's timeout matrix).
    run_in_background(orchestrator.run(incident.incident_id))

    return IncidentIngestResponse(incident_id=incident.incident_id, status=incident.status)


@router.post("/pipelines/{job_name}/check", response_model=PipelineHealthResponse)
async def check_pipeline(
    job_name: str,
    state_manager: StateManager = Depends(get_state_manager),
    orchestrator: WardenOrchestrator = Depends(get_orchestrator),
) -> PipelineHealthResponse:
    """On-demand health check for a real external pipeline (see
    app/agents/pipeline_health.py). If the monitored job's latest
    execution failed, opens a real incident (source="cloud_run_job") with
    the actual error line pulled from Cloud Logging and kicks off the
    same orchestrator loop the synthetic demo presets use -- this is the
    "detection" seam a future push-based trigger (Cloud Monitoring alert
    policy -> webhook) can replace without touching anything downstream.
    """
    result = await check_pipeline_health(job_name)
    if result.get("healthy", True):
        return PipelineHealthResponse(healthy=True, job_name=job_name, detail=result)

    settings = get_settings()
    incident = IncidentState(
        incident_id=new_id(),
        source="cloud_run_job",
        resource_uri=settings.warden_monitored_job_resource_uri,
        severity="P2",
        raw_event={
            "real_pipeline_failure": True,
            "job_name": result.get("job_name", job_name),
            "execution_name": result.get("execution_name"),
            "error_message": result.get("error_message"),
            "started_at": result.get("started_at"),
        },
        orchestrator_model=settings.warden_orchestrator_model,
    )
    await state_manager.create_incident(incident)
    run_in_background(orchestrator.run(incident.incident_id))

    return PipelineHealthResponse(
        healthy=False, job_name=job_name, incident_id=incident.incident_id, detail=result
    )


@router.get("/incidents", response_model=list[IncidentState])
async def list_incidents(
    limit: int = 200,
    state_manager: StateManager = Depends(get_state_manager),
) -> list[IncidentState]:
    """Newest-first incident history for the UI's history view -- every
    incident that was ever ingested, regardless of how it ended up
    (RESOLVED, REJECTED, FAILED, or still in flight)."""
    return await state_manager.list_incidents(limit=limit)


@router.get("/incidents/{incident_id}", response_model=IncidentState)
async def get_incident(
    incident_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> IncidentState:
    return await state_manager.get_incident(incident_id)


@router.get("/incidents/{incident_id}/steps", response_model=list[AgentStepLog])
async def list_incident_steps(
    incident_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> list[AgentStepLog]:
    await state_manager.get_incident(incident_id)  # 404s cleanly if unknown
    return await state_manager.list_steps(incident_id)


@router.get("/incidents/{incident_id}/findings", response_model=list[DiagnosticFinding])
async def list_incident_findings(
    incident_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> list[DiagnosticFinding]:
    await state_manager.get_incident(incident_id)
    return await state_manager.list_findings(incident_id)


@router.get("/incidents/{incident_id}/patches", response_model=list[SQLPatchPayload])
async def list_incident_patches(
    incident_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> list[SQLPatchPayload]:
    await state_manager.get_incident(incident_id)
    return await state_manager.list_patches(incident_id)


@router.get("/incidents/{incident_id}/audits", response_model=list[GovernanceAudit])
async def list_incident_audits(
    incident_id: str,
    state_manager: StateManager = Depends(get_state_manager),
) -> list[GovernanceAudit]:
    await state_manager.get_incident(incident_id)
    return await state_manager.list_audits(incident_id)


@router.post("/incidents/{incident_id}/execute", response_model=IncidentState)
async def execute(
    incident_id: str,
    payload: ExecuteRequest | None = None,
    state_manager: StateManager = Depends(get_state_manager),
) -> IncidentState:
    patch_id = payload.patch_id if payload else None
    return await execute_incident(
        incident_id, patch_id=patch_id, state_manager=state_manager
    )


@router.post("/incidents/{incident_id}/reject", response_model=IncidentState)
async def reject(
    incident_id: str,
    payload: RejectRequest | None = None,
    state_manager: StateManager = Depends(get_state_manager),
) -> IncidentState:
    reason = payload.reason if payload else None
    return await reject_incident(incident_id, reason=reason, state_manager=state_manager)
