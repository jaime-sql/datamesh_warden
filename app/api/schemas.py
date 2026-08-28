"""Request/response models for the HTTP surface (`app/api/routes.py`).

Kept separate from `app/models/state.py` so the wire contract can evolve
independently of the internal persistence schema -- e.g. `IncidentIngestRequest`
intentionally omits server-generated fields like `incident_id` and `status`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import IncidentSource, IncidentStatus, Severity


class IncidentIngestRequest(BaseModel):
    """A simplified CloudEvent-shaped payload.

    This project doesn't parse a full CNCF CloudEvents envelope
    (`specversion`/`type`/`id`/`time`/...) since every event source here is
    either a synthetic probe, a real GCP push subscription payload
    unwrapped upstream, or a Streamlit demo trigger -- just the fields
    needed to open an incident are exposed here.
    """

    model_config = ConfigDict(extra="forbid")

    source: IncidentSource
    resource_uri: str
    severity: Severity = "P2"
    raw_event: dict[str, Any] = Field(default_factory=dict)


class IncidentIngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    status: IncidentStatus


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch_id: str | None = None


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class PipelineHealthResponse(BaseModel):
    """Response for `POST /pipelines/{job_name}/check` -- see
    app/agents/pipeline_health.py. `incident_id` is only set when the
    check found a real failure and opened an incident for it."""

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    job_name: str
    incident_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str
