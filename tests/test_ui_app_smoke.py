"""End-to-end smoke tests for `ui/streamlit_app.py`, using Streamlit's own
`AppTest` harness against a *real* HTTP server.

A real `uvicorn` server (not `httpx.ASGITransport`) is spun up in a
background thread within this same process, because the UI talks to the
API purely over HTTP (`ui/api_client.py` uses a blocking `httpx.Client`,
not an ASGI transport) -- that's the whole point of Phase 5's
API-mediated design (see docs/architecture.md's Phase 5 implementation
note). The background orchestrator is stubbed out via the same
`get_orchestrator` dependency-override seam `tests/test_api.py` uses, so
this needs no Gemini credentials and no GCP resources, just a loopback
socket.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from streamlit.testing.v1 import AppTest

from app.api.deps import get_orchestrator
from app.api.main import app
from app.models import DiagnosticFinding, GovernanceAudit, SQLPatchPayload, new_id
from app.persistence import StateManager
from app.persistence.factory import get_state_manager

_APP_PATH = str(Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")


class _UiStubOrchestrator:
    """Deterministically populates a full findings/patch/audit trail and
    leaves the incident AWAITING_APPROVAL, so the UI has something
    interesting to render in every tab without calling Gemini."""

    def __init__(self, state_manager: StateManager) -> None:
        self._state_manager = state_manager

    async def run(self, incident_id: str) -> None:
        sm = self._state_manager

        finding = DiagnosticFinding(
            finding_id=new_id(),
            hypothesis="Column `email` was dropped from `orders`.",
            drift_type="SCHEMA_DRIFT",
            affected_columns=["email"],
            confidence=0.92,
            triage_model="stub",
        )
        await sm.write_finding(incident_id, finding)

        patch = SQLPatchPayload(
            patch_id=new_id(),
            linked_finding_id=finding.finding_id,
            patch_kind="DDL",
            sandbox_sql="ALTER TABLE sandbox.orders ADD COLUMN email STRING",
            production_sql="ALTER TABLE prod.orders ADD COLUMN email STRING",
            validation_status="SANDBOX_PASS",
            patcher_model="stub",
        )
        await sm.write_patch(incident_id, patch)

        audit = GovernanceAudit(
            audit_id=new_id(),
            linked_patch_id=patch.patch_id,
            verdict="PASS",
            rationale="No PII columns touched.",
        )
        await sm.write_audit(incident_id, audit)

        await sm.update_incident(
            incident_id, status="AWAITING_APPROVAL", summary="stub diagnosis complete"
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_api_base_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    state_manager = get_state_manager()
    app.dependency_overrides[get_orchestrator] = lambda: _UiStubOrchestrator(state_manager)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/status", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("live API server did not become ready in time")

    monkeypatch.setenv("WARDEN_API_BASE_URL", base_url)
    yield base_url

    server.should_exit = True
    thread.join(timeout=5)
    app.dependency_overrides.pop(get_orchestrator, None)


def test_landing_page_shows_empty_state(live_api_base_url: str) -> None:
    at = AppTest.from_file(_APP_PATH)
    at.run()

    assert not at.exception
    assert any("Trigger a demo incident" in md.value for md in at.markdown)


def test_preset_click_opens_incident_and_renders_all_tabs(live_api_base_url: str) -> None:
    at = AppTest.from_file(_APP_PATH)
    at.run()
    assert not at.exception

    schema_drift_button = next(b for b in at.sidebar.button if "Schema drift" in b.label)
    schema_drift_button.click().run()
    assert not at.exception

    incident_id = at.session_state["active_incident_id"]
    assert incident_id

    # The background orchestrator task runs on the live server's own event
    # loop; poll briefly until it lands on AWAITING_APPROVAL.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = httpx.get(f"{live_api_base_url}/incidents/{incident_id}")
        if response.json()["status"] == "AWAITING_APPROVAL":
            break
        time.sleep(0.1)

    at.run()
    assert not at.exception

    tab_labels = [tab.label for tab in at.tabs]
    assert tab_labels == ["🕒 Timeline", "🔎 Diagnosis", "🛠️ Patch diff", "⚖️ Governance"]

    approve_button = next(b for b in at.button if "Approve" in b.label)
    assert not approve_button.disabled


def test_incident_history_view_lists_incidents_with_status_counts(
    live_api_base_url: str,
) -> None:
    at = AppTest.from_file(_APP_PATH)
    at.run()
    assert not at.exception

    schema_drift_button = next(b for b in at.sidebar.button if "Schema drift" in b.label)
    schema_drift_button.click().run()
    incident_id = at.session_state["active_incident_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = httpx.get(f"{live_api_base_url}/incidents/{incident_id}")
        if response.json()["status"] == "AWAITING_APPROVAL":
            break
        time.sleep(0.1)
    at.run()

    at.sidebar.radio[0].set_value("📋 Incident History").run()
    assert not at.exception

    assert any("Incident History" in t.value for t in at.title)
    # Total / Resolved / Rejected / Failed / In progress.
    assert len(at.metric) == 5
    assert any(incident_id in str(df.value) for df in at.dataframe)


def test_opening_an_incident_from_history_switches_back_to_war_room(
    live_api_base_url: str,
) -> None:
    at = AppTest.from_file(_APP_PATH)
    at.run()

    schema_drift_button = next(b for b in at.sidebar.button if "Schema drift" in b.label)
    schema_drift_button.click().run()
    incident_id = at.session_state["active_incident_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = httpx.get(f"{live_api_base_url}/incidents/{incident_id}")
        if response.json()["status"] == "AWAITING_APPROVAL":
            break
        time.sleep(0.1)
    at.run()

    at.sidebar.radio[0].set_value("📋 Incident History").run()
    assert not at.exception

    at.selectbox(key="history_open_select").select(incident_id).run()
    open_button = next(b for b in at.button if "Open in War Room" in b.label)
    open_button.click().run()
    assert not at.exception

    assert at.session_state["active_incident_id"] == incident_id
    tab_labels = [tab.label for tab in at.tabs]
    assert tab_labels == ["🕒 Timeline", "🔎 Diagnosis", "🛠️ Patch diff", "⚖️ Governance"]
    assert at.sidebar.radio[0].value == "🚨 War Room"


def test_approve_button_executes_and_resolves_incident(live_api_base_url: str) -> None:
    at = AppTest.from_file(_APP_PATH)
    at.run()

    schema_drift_button = next(b for b in at.sidebar.button if "Schema drift" in b.label)
    schema_drift_button.click().run()
    incident_id = at.session_state["active_incident_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = httpx.get(f"{live_api_base_url}/incidents/{incident_id}")
        if response.json()["status"] == "AWAITING_APPROVAL":
            break
        time.sleep(0.1)
    at.run()

    approve_button = next(b for b in at.button if "Approve" in b.label)
    approve_button.click().run()
    assert not at.exception

    final = httpx.get(f"{live_api_base_url}/incidents/{incident_id}").json()
    assert final["status"] == "RESOLVED"
