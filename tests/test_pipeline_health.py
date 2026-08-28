from __future__ import annotations

import datetime
from collections.abc import Iterator

import pytest
from google.cloud import run_v2

from app.agents.pipeline_health import (
    CloudRunPipelineHealthBackend,
    LocalHeuristicPipelineHealthBackend,
    check_pipeline_health,
    get_pipeline_health_backend,
)
from app.config import get_settings


class _FakeLogEntry:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class _FakeLoggingClient:
    def __init__(self, entries: list[_FakeLogEntry]) -> None:
        self._entries = entries
        self.last_filter: str | None = None

    def list_entries(
        self, *, filter_: str, order_by: object, max_results: int
    ) -> list[_FakeLogEntry]:
        self.last_filter = filter_
        return self._entries


class _FakeExecutionsClient:
    def __init__(self, executions: list[run_v2.Execution]) -> None:
        self._executions = executions
        self.last_request: run_v2.ListExecutionsRequest | None = None

    def list_executions(
        self, request: run_v2.ListExecutionsRequest
    ) -> list[run_v2.Execution]:
        self.last_request = request
        return self._executions


def _execution(*, name: str, succeeded_count: int, failed_count: int) -> run_v2.Execution:
    execution = run_v2.Execution(
        name=name, succeeded_count=succeeded_count, failed_count=failed_count
    )
    execution.start_time = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    return execution


@pytest.fixture(autouse=True)
def _google_cloud_project(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # CloudRunPipelineHealthBackend requires a project to build the Cloud
    # Run Admin API "parent" path -- set a fixed one for every test in this
    # file so they don't depend on (or leak into) real environment state.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_local_heuristic_backend_always_reports_healthy() -> None:
    backend = LocalHeuristicPipelineHealthBackend()
    result = await backend.check("pg-to-bq-sync")
    assert result["healthy"] is True
    assert result["job_name"] == "pg-to-bq-sync"


async def test_cloud_run_backend_reports_healthy_when_latest_execution_succeeded() -> None:
    execution = _execution(
        name="projects/p/locations/us-central1/jobs/pg-to-bq-sync/executions/pg-to-bq-sync-kxnx9",
        succeeded_count=1,
        failed_count=0,
    )
    backend = CloudRunPipelineHealthBackend(
        executions_client=_FakeExecutionsClient([execution]),
        logging_client=_FakeLoggingClient([]),
    )
    result = await backend.check("pg-to-bq-sync")
    assert result["healthy"] is True
    assert result["execution_name"] == "pg-to-bq-sync-kxnx9"


async def test_cloud_run_backend_reports_unhealthy_and_pulls_real_error_line() -> None:
    execution = _execution(
        name="projects/p/locations/us-central1/jobs/pg-to-bq-sync/executions/pg-to-bq-sync-72ml6",
        succeeded_count=0,
        failed_count=1,
    )
    logs = _FakeLoggingClient(
        [
            _FakeLogEntry("Connecting to source Postgres..."),
            _FakeLogEntry('SYNC FAILED: UndefinedColumn: column "telefono" does not exist'),
        ]
    )
    backend = CloudRunPipelineHealthBackend(
        executions_client=_FakeExecutionsClient([execution]), logging_client=logs
    )
    result = await backend.check("pg-to-bq-sync")
    assert result["healthy"] is False
    assert result["execution_name"] == "pg-to-bq-sync-72ml6"
    assert "SYNC FAILED" in result["error_message"]
    assert result["started_at"]
    assert logs.last_filter is not None
    assert "pg-to-bq-sync-72ml6" in logs.last_filter  # scoped to this execution, not the whole job


async def test_cloud_run_backend_falls_back_to_last_line_when_no_sync_failed_marker() -> None:
    execution = _execution(
        name="projects/p/locations/us-central1/jobs/pg-to-bq-sync/executions/pg-to-bq-sync-zzz",
        succeeded_count=0,
        failed_count=1,
    )
    logs = _FakeLoggingClient([_FakeLogEntry("some unrelated stderr noise")])
    backend = CloudRunPipelineHealthBackend(
        executions_client=_FakeExecutionsClient([execution]), logging_client=logs
    )
    result = await backend.check("pg-to-bq-sync")
    assert result["healthy"] is False
    assert result["error_message"] == "some unrelated stderr noise"


async def test_cloud_run_backend_reports_healthy_when_no_executions_exist() -> None:
    backend = CloudRunPipelineHealthBackend(
        executions_client=_FakeExecutionsClient([]), logging_client=_FakeLoggingClient([])
    )
    result = await backend.check("pg-to-bq-sync")
    assert result["healthy"] is True
    assert "note" in result


def test_get_pipeline_health_backend_selects_by_warden_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_MODE", "local")
    get_settings.cache_clear()
    try:
        assert isinstance(get_pipeline_health_backend(), LocalHeuristicPipelineHealthBackend)
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("WARDEN_MODE", "cloud")
    get_settings.cache_clear()
    try:
        assert isinstance(get_pipeline_health_backend(), CloudRunPipelineHealthBackend)
    finally:
        get_settings.cache_clear()
        monkeypatch.setenv("WARDEN_MODE", "local")


async def test_check_pipeline_health_defaults_to_configured_job_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_MODE", "local")
    get_settings.cache_clear()
    try:
        result = await check_pipeline_health()
        assert result["job_name"] == get_settings().warden_monitored_job_name
    finally:
        get_settings.cache_clear()
