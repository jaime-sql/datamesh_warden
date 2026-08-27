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

import os
from collections.abc import Callable
from typing import Any, cast

import httpx

IdTokenProvider = Callable[[str], "str | None"]


class WardenApiError(RuntimeError):
    """Raised when the API responds with a non-2xx status. `detail` carries
    the JSON error body's `detail` field when the response has one."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def fetch_cloud_run_id_token(audience: str) -> str | None:
    """Fetches a Google-signed identity token for service-to-service auth
    against a private (`--no-allow-unauthenticated`) Cloud Run service
    (see docs/architecture.md's Phase 6 note on the private-API design).

    Only attempted when actually running on Cloud Run (`K_SERVICE` is set
    by the runtime) or explicitly opted into via
    `WARDEN_API_USE_ID_TOKEN=true` -- local dev against a public/local API
    never needs this and shouldn't pay for a metadata-server round trip on
    every rerun.
    """
    if not (os.environ.get("K_SERVICE") or os.environ.get("WARDEN_API_USE_ID_TOKEN") == "true"):
        return None

    import google.auth.transport.requests
    import google.oauth2.id_token

    request = google.auth.transport.requests.Request()
    return cast(
        "str", google.oauth2.id_token.fetch_id_token(request, audience)  # type: ignore[no-untyped-call]
    )


class WardenApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        id_token_provider: IdTokenProvider | None = fetch_cloud_run_id_token,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._id_token_provider = id_token_provider
        self._client = httpx.Client(
            base_url=self._base_url, timeout=timeout_s, transport=transport
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

    def _auth_headers(self) -> dict[str, str]:
        if self._id_token_provider is None:
            return {}
        token = self._id_token_provider(self._base_url)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._auth_headers()
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
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
