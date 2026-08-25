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
    format_timestamp,
    policy_result_icon,
    status_badge,
    step_kind_icon,
    step_status_icon,
    verdict_badge,
)
from ui.presets import PRESETS


def render_sidebar() -> dict[str, Any] | None:
    """Renders the sidebar. Returns an ingest payload dict if the user just
    fired a preset or custom event this run, else None."""
    st.sidebar.title("🛡️ DataMesh Warden")
    st.sidebar.caption("Autonomous data-incident war room")

    st.sidebar.subheader("Trigger a demo incident")
    fired: dict[str, Any] | None = None
    for preset in PRESETS:
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
            "Resource URI", value="bq://warden-demo.sales.orders", key="custom_resource_uri"
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
    st.sidebar.subheader("Load an existing incident")
    loaded_id = st.sidebar.text_input("Incident ID", key="load_incident_id")
    if st.sidebar.button("Load", use_container_width=True) and loaded_id:
        st.session_state["active_incident_id"] = loaded_id

    return fired


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
