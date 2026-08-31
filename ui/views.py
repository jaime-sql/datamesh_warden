"""Streamlit rendering functions for the DataMesh Warden war room.

Split out of `streamlit_app.py` so the entrypoint stays a thin wiring
script. These functions are side-effecting (`st.*` calls) by nature, so
they're exercised via `AppTest`-based smoke tests
(`tests/test_ui_app_smoke.py`) rather than plain unit tests -- the actual
decision logic they depend on lives in `ui/formatting.py`, which *is*
plain-unit-testable.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.formatting import (
    can_execute,
    format_full_timestamp,
    format_timestamp,
    incident_status_counts,
    policy_result_icon,
    status_badge,
    step_kind_icon,
    step_status_icon,
    verdict_badge,
)
from ui.presets import IncidentPreset

VIEW_WAR_ROOM = "🚨 War Room"
VIEW_HISTORY = "📋 Incident History"


def render_sidebar(
    presets: list[IncidentPreset], *, monitored_job_name: str
) -> tuple[str, dict[str, Any] | None, bool]:
    """Renders the sidebar. Returns `(selected_view, fired, check_pipeline)`:
    `fired` is an ingest payload dict if the user just fired a preset or
    custom event this run (else None); `check_pipeline` is True if the
    user just clicked the real-pipeline health-check button."""
    # Must run before the `warden_view` radio widget below is instantiated:
    # Streamlit forbids writing to a widget's session_state key in the same
    # run it was created in, so switching the radio's selection (e.g. after
    # firing a preset) has to happen via this one-run-delayed handoff key
    # instead of setting `warden_view` directly.
    if "_force_view" in st.session_state:
        st.session_state["warden_view"] = st.session_state.pop("_force_view")

    st.sidebar.title("🛡️ DataMesh Warden")
    st.sidebar.caption("Autonomous data-incident war room")

    view = st.sidebar.radio(
        "View", [VIEW_WAR_ROOM, VIEW_HISTORY], key="warden_view", horizontal=True
    )

    st.sidebar.divider()
    st.sidebar.subheader("Open an incident")
    fired: dict[str, Any] | None = None
    for preset in presets:
        if st.sidebar.button(
            f"{preset['icon']} {preset['label']}", use_container_width=True
        ):
            fired = {
                "source": preset["source"],
                "resource_uri": preset["resource_uri"],
                "severity": preset["severity"],
                "raw_event": preset["raw_event"],
            }

    with st.sidebar.expander("Custom event"):
        source = st.selectbox(
            "Source",
            ["bigquery_audit", "cloudsql_alert", "synthetic_probe", "manual_demo"],
            key="custom_source",
        )
        resource_uri = st.text_input(
            "Resource URI", value=presets[0]["resource_uri"], key="custom_resource_uri"
        )
        severity = st.selectbox("Severity", ["P1", "P2", "P3"], key="custom_severity")
        raw_event_json = st.text_area(
            "Raw event (JSON)", value="{}", key="custom_raw_event"
        )
        if st.button("Fire custom event", use_container_width=True):
            import json

            try:
                raw_event = json.loads(raw_event_json)
            except ValueError as exc:
                st.error(f"Invalid JSON: {exc}")
            else:
                fired = {
                    "source": source,
                    "resource_uri": resource_uri,
                    "severity": severity,
                    "raw_event": raw_event,
                }

    st.sidebar.divider()
    st.sidebar.subheader("Real pipeline")
    st.sidebar.caption(
        f"Checks the real `{monitored_job_name}` Cloud Run Job "
        "(datamesh_pipeline) and opens a real incident if its latest run failed."
    )
    check_pipeline = st.sidebar.button(
        "🔌 Check pipeline health", use_container_width=True
    )

    st.sidebar.divider()
    st.sidebar.subheader("Load an existing incident")
    loaded_id = st.sidebar.text_input("Incident ID", key="load_incident_id")
    if st.sidebar.button("Load", use_container_width=True) and loaded_id:
        st.session_state["active_incident_id"] = loaded_id
        st.session_state["_force_view"] = VIEW_WAR_ROOM
        view = VIEW_WAR_ROOM

    if fired is not None:
        st.session_state["_force_view"] = VIEW_WAR_ROOM
        view = VIEW_WAR_ROOM

    return view, fired, check_pipeline


def render_header(incident: dict[str, Any]) -> None:
    cols = st.columns([3, 1, 1, 1])
    with cols[0]:
        st.subheader(f"Incident `{incident['incident_id']}`")
        st.caption(incident["resource_uri"])
    with cols[1]:
        st.metric("Status", status_badge(incident["status"]))
    with cols[2]:
        st.metric("Severity", incident["severity"])
    with cols[3]:
        st.metric("Turns", incident["turn_count"])
    if incident.get("summary"):
        st.info(incident["summary"])
    if incident.get("error"):
        st.error(incident["error"])


def render_timeline(steps: list[dict[str, Any]]) -> None:
    if not steps:
        st.caption("No steps logged yet.")
        return
    for step in steps:
        icon = step_kind_icon(step["kind"])
        status_icon = step_status_icon(step["status"])
        title = f"{icon} {step['kind']} — {step['actor']} {status_icon}"
        if step.get("tool_name"):
            title += f" (`{step['tool_name']}`)"
        with st.expander(title, expanded=step["status"] == "ERROR"):
            st.caption(
                f"started {format_timestamp(step['started_at'])}"
                f" · finished {format_timestamp(step.get('finished_at'))}"
                + (f" · {step['latency_ms']} ms" if step.get("latency_ms") is not None else "")
            )
            if step.get("content_markdown"):
                st.markdown(step["content_markdown"])
            if step.get("tool_args"):
                st.json(step["tool_args"])
            if step.get("error_detail"):
                st.error(step["error_detail"])


def render_diagnosis(findings: list[dict[str, Any]]) -> None:
    if not findings:
        st.caption("No diagnostic findings yet.")
        return
    for finding in findings:
        with st.container(border=True):
            st.markdown(f"**{finding['drift_type']}** — confidence {finding['confidence']:.0%}")
            st.write(finding["hypothesis"])
            if finding.get("affected_columns"):
                st.caption("Affected columns: " + ", ".join(finding["affected_columns"]))
            if finding.get("evidence"):
                with st.expander(f"Evidence ({len(finding['evidence'])})"):
                    for item in finding["evidence"]:
                        st.text(f"[{item['severity']}] {item['source']}: {item['log_line']}")


def render_patch_diff(patches: list[dict[str, Any]]) -> None:
    if not patches:
        st.caption("No patch generated yet.")
        return
    for patch in patches:
        with st.container(border=True):
            st.markdown(
                f"**Patch `{patch['patch_id']}`** ({patch['patch_kind']}) — "
                f"{patch['validation_status']}"
            )
            cols = st.columns(2)
            with cols[0]:
                st.caption("Sandbox SQL")
                st.code(patch["sandbox_sql"], language="sql")
            with cols[1]:
                st.caption("Production SQL")
                st.code(patch["production_sql"], language="sql")
            st.caption(
                f"Dry-run bytes: {patch['dry_run_bytes_processed']:,} · "
                f"Sandbox latency: {patch['sandbox_execution_ms']} ms · "
                f"Row delta: {patch['sandbox_row_delta']:+d}"
            )
            if patch.get("validation_errors"):
                for err in patch["validation_errors"]:
                    st.error(err)


def render_governance(audits: list[dict[str, Any]]) -> None:
    if not audits:
        st.caption("No governance audit yet.")
        return
    for audit in audits:
        with st.container(border=True):
            st.markdown(
                f"**{verdict_badge(audit['verdict'])}** for patch `{audit['linked_patch_id']}`"
            )
            st.write(audit["rationale"])
            if audit.get("pii_columns_touched"):
                st.warning("PII columns touched: " + ", ".join(audit["pii_columns_touched"]))
            if audit.get("policy_checks"):
                for check in audit["policy_checks"]:
                    st.text(
                        f"{policy_result_icon(check['result'])} {check['policy_id']}: "
                        f"{check['detail']}"
                    )


def render_incident_history(incidents: list[dict[str, Any]]) -> str | None:
    """Renders the full incident history: every incident ever ingested,
    regardless of how it ended up (resolved, rejected, failed, or still in
    flight), plus summary counts by status. Returns an incident_id if the
    user picked one to open in the War Room this run, else None."""
    st.title("📋 Incident History")

    if not incidents:
        st.caption("No incidents have been ingested yet.")
        return None

    counts = incident_status_counts(incidents)
    total = len(incidents)
    resolved = counts.get("RESOLVED", 0)
    rejected = counts.get("REJECTED", 0)
    failed = counts.get("FAILED", 0)
    in_flight = total - resolved - rejected - failed

    cols = st.columns(5)
    cols[0].metric("Total incidents", total)
    cols[1].metric("✅ Resolved", resolved)
    cols[2].metric("🚫 Rejected", rejected)
    cols[3].metric("❌ Failed", failed)
    cols[4].metric("⏳ In progress", in_flight)

    st.divider()

    st.dataframe(
        [
            {
                "Incident ID": incident["incident_id"],
                "Status": status_badge(incident["status"]),
                "Source": incident["source"],
                "Resource": incident["resource_uri"],
                "Severity": incident["severity"],
                "Created": format_full_timestamp(incident["created_at"]),
                "Updated": format_full_timestamp(incident["updated_at"]),
            }
            for incident in incidents
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Open an incident")
    incident_ids = [incident["incident_id"] for incident in incidents]
    selected = st.selectbox("Incident ID", incident_ids, key="history_open_select")
    if st.button("Open in War Room", key="history_open_button") and selected:
        return str(selected)
    return None


def render_decision_footer(incident: dict[str, Any], audits: list[dict[str, Any]]) -> str | None:
    """Renders the Approve/Reject footer. Returns 'execute', 'reject', or
    None depending on what the user just clicked."""
    if incident["status"] != "AWAITING_APPROVAL":
        return None

    st.divider()
    executable = can_execute(incident["status"], audits)
    cols = st.columns(2)
    action: str | None = None
    with cols[0]:
        if st.button(
            "✅ Approve & execute",
            type="primary",
            use_container_width=True,
            disabled=not executable,
        ):
            action = "execute"
        if not executable:
            st.caption("Blocked by governance -- cannot execute.")
    with cols[1]:
        if st.button("🚫 Reject", use_container_width=True):
            action = "reject"
    return action
