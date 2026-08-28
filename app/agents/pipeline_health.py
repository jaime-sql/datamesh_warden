"""Post-Phase-6 addition: on-demand health check for a real external
pipeline (the separate `datamesh_pipeline` project's `pg-to-bq-sync` Cloud
Run Job, deployed into the same GCP project as Warden).

This is the "detection" half of connecting Warden to a *real* incident
source instead of the four synthetic demo presets: `check_pipeline_health`
looks at the monitored Cloud Run Job's most recent execution, and if it
failed, pulls the real error line out of Cloud Logging so the resulting
incident's `raw_event` carries genuine evidence -- not a canned scenario
string.

Deliberately manual/on-demand (a UI button, or `POST
/pipelines/{job_name}/check`) rather than push-based (a Cloud Monitoring
alert policy -> webhook), mirroring the same "skip Eventarc for now"
decision made for the synthetic scenarios back in Phase 6 -- see
docs/architecture.md. A push-based trigger can replace this call site
later without touching anything downstream of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Protocol

from google.cloud import logging_v2, run_v2

from app.config import get_settings


class PipelineHealthBackend(Protocol):
    async def check(self, job_name: str) -> dict[str, Any]: ...


class _ExecutionsClientLike(Protocol):
    """Narrow interface `CloudRunPipelineHealthBackend` actually needs from
    `run_v2.ExecutionsClient` -- lets tests inject a plain fake instead of
    a real (network-calling) client, without mypy complaining that the
    fake isn't literally an `ExecutionsClient` subclass."""

    def list_executions(
        self, request: run_v2.ListExecutionsRequest
    ) -> Iterable[run_v2.Execution]: ...


class _LoggingClientLike(Protocol):
    """Narrow interface `CloudRunPipelineHealthBackend` actually needs from
    `logging_v2.Client` -- see `_ExecutionsClientLike` above."""

    def list_entries(
        self, *, filter_: str, order_by: str, max_results: int
    ) -> Iterable[Any]: ...


class LocalHeuristicPipelineHealthBackend:
    """Zero-resource fallback: there's no real Cloud Run Job to check in
    local/offline dev, so this always reports healthy. Exists purely so
    the API route and UI button work end-to-end without real GCP access."""

    async def check(self, job_name: str) -> dict[str, Any]:
        return {
            "healthy": True,
            "job_name": job_name,
            "note": "Simulated: WARDEN_MODE=local has no real Cloud Run Job to check.",
        }


class CloudRunPipelineHealthBackend:
    """Real implementation: reads the monitored job's latest execution via
    the Cloud Run Admin API, and -- only if it failed -- pulls the actual
    failure line out of Cloud Logging.

    Requires the caller's identity (`warden-api-run` in production) to
    hold `roles/run.viewer` and `roles/logging.viewer` on the project the
    monitored job lives in. Both are read-only roles; this backend never
    writes to the monitored pipeline or its logs.
    """

    def __init__(
        self,
        *,
        executions_client: _ExecutionsClientLike | None = None,
        logging_client: _LoggingClientLike | None = None,
    ) -> None:
        # Both injectable purely for tests -- production always lazily
        # constructs real clients on first use via the properties below.
        self._executions_client = executions_client
        self._logging_client = logging_client

    def _executions(self) -> _ExecutionsClientLike:
        if self._executions_client is None:
            self._executions_client = run_v2.ExecutionsClient()
        return self._executions_client

    def _logging(self, project: str) -> _LoggingClientLike:
        if self._logging_client is None:
            self._logging_client = logging_v2.Client(project=project)  # type: ignore[no-untyped-call]
        return self._logging_client

    async def check(self, job_name: str) -> dict[str, Any]:
        settings = get_settings()
        project = settings.google_cloud_project
        if not project:
            # Only reachable via misconfiguration: get_pipeline_health_backend
            # only selects this backend when WARDEN_MODE=cloud, which always
            # implies GOOGLE_CLOUD_PROJECT is set.
            raise RuntimeError("GOOGLE_CLOUD_PROJECT must be set to check a real pipeline")
        region = settings.warden_monitored_job_region
        parent = f"projects/{project}/locations/{region}/jobs/{job_name}"

        def _list_latest() -> run_v2.Execution | None:
            request = run_v2.ListExecutionsRequest(parent=parent, page_size=1)
            for execution in self._executions().list_executions(request):
                return execution
            return None

        latest = await asyncio.to_thread(_list_latest)
        if latest is None:
            return {
                "healthy": True,
                "job_name": job_name,
                "note": "No executions found yet for this job.",
            }

        execution_name = latest.name.rsplit("/", 1)[-1]

        if latest.failed_count == 0:
            return {
                "healthy": True,
                "job_name": job_name,
                "execution_name": execution_name,
                "succeeded_count": latest.succeeded_count,
            }

        error_message = await asyncio.to_thread(
            self._fetch_error_line, project, job_name, execution_name
        )
        started_at = latest.start_time.rfc3339() if latest.start_time else None

        return {
            "healthy": False,
            "job_name": job_name,
            "execution_name": execution_name,
            "failed_count": latest.failed_count,
            "error_message": error_message,
            "started_at": started_at,
        }

    def _fetch_error_line(self, project: str, job_name: str, execution_name: str) -> str:
        # `run.googleapis.com/execution_name` is the label Cloud Run stamps
        # on every log entry emitted by a given job execution -- scoping to
        # it (rather than just the job name) avoids picking up a stale
        # error line from a *previous* failed run.
        filter_ = (
            'resource.type="cloud_run_job" '
            f'AND resource.labels.job_name="{job_name}" '
            f'AND labels."run.googleapis.com/execution_name"="{execution_name}"'
        )
        entries = self._logging(project).list_entries(
            filter_=filter_, order_by=str(logging_v2.DESCENDING), max_results=50
        )
        for entry in entries:
            payload = entry.payload
            text = payload if isinstance(payload, str) else str(payload)
            # job/sync.py's own final line on failure -- prefer it verbatim
            # over any other stderr noise (tracebacks, driver warnings).
            if "SYNC FAILED" in text.upper():
                return text
        for entry in entries:
            payload = entry.payload
            if isinstance(payload, str) and payload.strip():
                return payload
        return (
            "Execution failed but no readable log line was found for it -- "
            "check Cloud Logging directly."
        )


def get_pipeline_health_backend() -> PipelineHealthBackend:
    settings = get_settings()
    if settings.warden_mode == "cloud":
        return CloudRunPipelineHealthBackend()
    return LocalHeuristicPipelineHealthBackend()


async def check_pipeline_health(job_name: str | None = None) -> dict[str, Any]:
    """Entry point used by the API route. Defaults to the configured
    monitored job (`WARDEN_MONITORED_JOB_NAME`) if none is given."""
    settings = get_settings()
    resolved_job_name = job_name or settings.warden_monitored_job_name
    backend = get_pipeline_health_backend()
    return await backend.check(resolved_job_name)
