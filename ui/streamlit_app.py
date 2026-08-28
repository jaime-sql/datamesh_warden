"""DataMesh Warden -- Streamlit war room (see docs/architecture.md section 5).

Entrypoint script only: wires the sidebar, header, tabs and decision footer
(all in `ui/views.py`) to the HTTP API (`ui/api_client.py`). Run with
`make run-ui` (needs `make run-api` running alongside it) or
`streamlit run ui/streamlit_app.py`.
"""

from __future__ import annotations

import os

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from app.config import get_settings
from ui.api_client import WardenApiClient, WardenApiError
from ui.formatting import is_in_flight
from ui.presets import DEFAULT_PROJECT, build_presets
from ui.views import (
    VIEW_HISTORY,
    VIEW_WAR_ROOM,
    render_decision_footer,
    render_diagnosis,
    render_governance,
    render_header,
    render_incident_history,
    render_patch_diff,
    render_sidebar,
    render_timeline,
)

st.set_page_config(
    page_title="DataMesh Warden",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def _api_client(base_url: str) -> WardenApiClient:
    return WardenApiClient(base_url)


def _get_client() -> WardenApiClient:
    base_url = os.environ.get("WARDEN_API_BASE_URL") or get_settings().warden_api_base_url
    return _api_client(base_url)


def main() -> None:
    client = _get_client()
    st.session_state.setdefault("active_incident_id", None)

    presets = build_presets(project=get_settings().google_cloud_project or DEFAULT_PROJECT)
    monitored_job_name = get_settings().warden_monitored_job_name
    view, fired, check_pipeline = render_sidebar(presets, monitored_job_name=monitored_job_name)
    if fired is not None:
        try:
            result = client.ingest_event(**fired)
        except WardenApiError as exc:
            st.sidebar.error(f"Could not ingest event: {exc.detail}")
        else:
            st.session_state["active_incident_id"] = result["incident_id"]
            st.rerun()

    if check_pipeline:
        try:
            health = client.check_pipeline_health(monitored_job_name)
        except WardenApiError as exc:
            st.sidebar.error(f"Health check failed: {exc.detail}")
        else:
            if health["healthy"]:
                st.sidebar.success(f"✅ `{monitored_job_name}` is healthy.")
            else:
                st.sidebar.warning(f"⚠️ `{monitored_job_name}` failed -- incident opened.")
                st.session_state["active_incident_id"] = health["incident_id"]
                st.session_state["_force_view"] = VIEW_WAR_ROOM
                st.rerun()

    if view == VIEW_HISTORY:
        try:
            incidents = client.list_incidents()
        except WardenApiError as exc:
            st.error(f"Could not load incident history: {exc.detail}")
            return
        opened = render_incident_history(incidents)
        if opened is not None:
            st.session_state["active_incident_id"] = opened
            st.session_state["_force_view"] = VIEW_WAR_ROOM
            st.rerun()
        return

    incident_id = st.session_state.get("active_incident_id")
    if not incident_id:
        st.title("🛡️ DataMesh Warden")
        st.write(
            "Trigger a demo incident from the sidebar, or load an existing "
            "incident by ID, to open the war room."
        )
        return

    try:
        incident = client.get_incident(incident_id)
    except WardenApiError as exc:
        st.error(f"Could not load incident `{incident_id}`: {exc.detail}")
        return

    if is_in_flight(incident["status"]):
        st_autorefresh(interval=1500, key="warden_autorefresh")

    render_header(incident)

    steps = client.list_steps(incident_id)
    findings = client.list_findings(incident_id)
    patches = client.list_patches(incident_id)
    audits = client.list_audits(incident_id)

    tab_timeline, tab_diagnosis, tab_patch, tab_governance = st.tabs(
        ["🕒 Timeline", "🔎 Diagnosis", "🛠️ Patch diff", "⚖️ Governance"]
    )
    with tab_timeline:
        render_timeline(steps)
    with tab_diagnosis:
        render_diagnosis(findings)
    with tab_patch:
        render_patch_diff(patches)
    with tab_governance:
        render_governance(audits)

    action = render_decision_footer(incident, audits)
    if action == "execute":
        try:
            client.execute(incident_id)
        except WardenApiError as exc:
            st.error(f"Execution failed: {exc.detail}")
        else:
            st.rerun()
    elif action == "reject":
        try:
            client.reject(incident_id, reason="rejected from war room UI")
        except WardenApiError as exc:
            st.error(f"Rejection failed: {exc.detail}")
        else:
            st.rerun()


main()
