"""Tests for `ui/api_client.py`, using `httpx.MockTransport` so no real
network call (or running API server) is needed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ui.api_client import WardenApiClient, WardenApiError


def _client(handler: Any) -> WardenApiClient:
    return WardenApiClient("http://test", transport=httpx.MockTransport(handler))


def test_ingest_event_posts_expected_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(202, json={"incident_id": "inc-1", "status": "INGESTED"})

    result = _client(handler).ingest_event(
        source="synthetic_probe",
        resource_uri="bq://p.d.t",
        severity="P1",
        raw_event={"scenario": "schema_drift"},
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://test/events/ingest"
    assert result == {"incident_id": "inc-1", "status": "INGESTED"}


def test_get_incident_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/incidents/inc-1"
        return httpx.Response(200, json={"incident_id": "inc-1", "status": "RESOLVED"})

    result = _client(handler).get_incident("inc-1")
    assert result["status"] == "RESOLVED"


@pytest.mark.parametrize(
    ("method_name", "path_suffix"),
    [
        ("list_steps", "steps"),
        ("list_findings", "findings"),
        ("list_patches", "patches"),
        ("list_audits", "audits"),
    ],
)
def test_list_subresource_helpers_hit_expected_path(method_name: str, path_suffix: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/incidents/inc-1/{path_suffix}"
        return httpx.Response(200, json=[])

    client = _client(handler)
    method = getattr(client, method_name)
    assert method("inc-1") == []


def test_execute_sends_patch_id_when_provided() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"incident_id": "inc-1", "status": "RESOLVED"})

    _client(handler).execute("inc-1", patch_id="patch-1")
    assert b'"patch_id":"patch-1"' in captured["body"]


def test_execute_omits_patch_id_when_absent() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"incident_id": "inc-1", "status": "RESOLVED"})

    _client(handler).execute("inc-1")
    assert captured["body"] == b"{}"


def test_reject_sends_reason() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"incident_id": "inc-1", "status": "REJECTED"})

    _client(handler).reject("inc-1", reason="too risky")
    assert b'"reason":"too risky"' in captured["body"]


def test_error_response_with_json_detail_raises_warden_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "incident not found: inc-1"})

    with pytest.raises(WardenApiError) as exc_info:
        _client(handler).get_incident("inc-1")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "incident not found: inc-1"


def test_error_response_without_json_body_falls_back_to_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(WardenApiError) as exc_info:
        _client(handler).get_incident("inc-1")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "boom"


def test_network_error_is_wrapped_in_warden_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(WardenApiError) as exc_info:
        _client(handler).get_incident("inc-1")

    assert exc_info.value.status_code == 0
    assert "connection refused" in exc_info.value.detail
