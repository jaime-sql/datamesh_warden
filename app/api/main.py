"""FastAPI application entrypoint: `app.api.main:app`.

Run locally with `make run-api` (`uvicorn app.api.main:app --reload`), or
via the Dockerfile for Cloud Run (see docs/architecture.md section 1,
"Cloud Run Service: warden-orchestrator").
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agents.executor import (
    GovernanceBlockError,
    IncidentNotAwaitingApprovalError,
    NoGovernanceAuditError,
    NoPatchAvailableError,
)
from app.api.routes import router
from app.api.schemas import HealthResponse
from app.logging import configure_logging, get_logger
from app.persistence.base import IncidentNotFoundError, PatchNotFoundError

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info("warden_api_startup")
    yield
    logger.info("warden_api_shutdown")


app = FastAPI(
    title="DataMesh Warden",
    description=(
        "Async, event-driven agent fleet that detects BigQuery and pipeline "
        "drift and orchestrates sub-agents to diagnose, patch, and govern "
        "the fix before a human approves execution."
    ),
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/status", response_model=HealthResponse)
async def status() -> HealthResponse:
    # Deliberately not "/healthz": Google's edge (GFE) intercepts that exact
    # path for its own infrastructure and never forwards it to the
    # container on Cloud Run -- discovered while validating the Phase 6
    # deploy (see docs/architecture.md's Phase 6 note).
    return HealthResponse()


@app.exception_handler(IncidentNotFoundError)
async def _incident_not_found_handler(
    request: Request, exc: IncidentNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"incident not found: {exc}"})


@app.exception_handler(PatchNotFoundError)
async def _patch_not_found_handler(request: Request, exc: PatchNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"patch not found: {exc}"})


@app.exception_handler(IncidentNotAwaitingApprovalError)
@app.exception_handler(NoPatchAvailableError)
@app.exception_handler(NoGovernanceAuditError)
@app.exception_handler(GovernanceBlockError)
async def _conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})
