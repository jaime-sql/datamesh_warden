"""Thin synchronous HTTP client the Streamlit UI uses to talk to the
DataMesh Warden API (`app/api/*`).

The UI never touches Firestore, BigQuery, or Gemini directly -- it only
ever calls this API, which behaves identically whether the API itself is
running with `WARDEN_MODE=local` or `WARDEN_MODE=cloud`. That's a
deliberate generalization of the "Streamlit never calls Gemini directly"
guarantee in docs/architecture.md section 1: here, it never calls *any*
backend directly, which is what lets the whole UI be built and tested
without any GCP resources (see the Phase 5 implementation note in
docs/architecture.md).

Synchronous (`httpx.Client`, not `AsyncClient`) because Streamlit's
script-rerun execution model doesn't run its own asyncio event loop.
"""

from __future__ import annotations

from typing import Any, cast

import httpx


class WardenApiError(RuntimeError):
    """Raised when the API responds with a non-2xx status. `detail` carries
    the JSON error body's `detail` field when the response has one."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class WardenApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WardenApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def ingest_event(
        self,
        *,
        source: str,
        resource_uri: str,
        severity: str,
        raw_event: dict[str, Any],
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._request(
                "POST",
                "/events/ingest",
                json={
                    "source": source,
                    "resource_uri": resource_uri,
                    "severity": severity,
                    "raw_event": raw_event,
                },
            ),
        )

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._request("GET", f"/incidents/{incident_id}"))

    def list_steps(self, incident_id: str) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", self._request("GET", f"/incidents/{incident_id}/steps")
        )

    def list_findings(self, incident_id: str) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", self._request("GET", f"/incidents/{incident_id}/findings")
        )

    def list_patches(self, incident_id: str) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", self._request("GET", f"/incidents/{incident_id}/patches")
        )

    def list_audits(self, incident_id: str) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", self._request("GET", f"/incidents/{incident_id}/audits")
        )

    def execute(self, incident_id: str, *, patch_id: str | None = None) -> dict[str, Any]:
        payload = {"patch_id": patch_id} if patch_id else {}
        return cast(
            "dict[str, Any]",
            self._request("POST", f"/incidents/{incident_id}/execute", json=payload),
        )

    def reject(self, incident_id: str, *, reason: str | None = None) -> dict[str, Any]:
        payload = {"reason": reason} if reason else {}
        return cast(
            "dict[str, Any]",
            self._request("POST", f"/incidents/{incident_id}/reject", json=payload),
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WardenApiError(0, f"could not reach the API: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text
            try:
                body = response.json()
                if isinstance(body, dict) and "detail" in body:
                    detail = str(body["detail"])
            except ValueError:
                pass
            raise WardenApiError(response.status_code, detail)

        return response.json()
