"""Pure formatting/business-logic helpers for the Streamlit UI.

Deliberately free of any `streamlit` import so these are plain, fast unit
tests -- no `AppTest` machinery needed to exercise them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_STATUS_BADGES: dict[str, str] = {
    "INGESTED": "🟡 Ingested",
    "DIAGNOSING": "🔎 Diagnosing",
    "PATCHING": "🛠️ Patching",
    "AWAITING_APPROVAL": "🟠 Awaiting approval",
    "EXECUTING": "⚙️ Executing",
    "RESOLVED": "✅ Resolved",
    "REJECTED": "🚫 Rejected",
    "FAILED": "❌ Failed",
}

_STEP_KIND_ICONS: dict[str, str] = {
    "MODEL_TURN": "🧠",
    "TOOL_CALL": "🛎️",
    "TOOL_RESULT": "📦",
    "USER_DECISION": "🙋",
    "EXECUTION": "⚡",
}

_STEP_STATUS_ICONS: dict[str, str] = {
    "RUNNING": "⏳",
    "OK": "✅",
    "ERROR": "⚠️",
    "TIMEOUT": "⌛",
}

_VERDICT_BADGES: dict[str, str] = {
    "PASS": "🟢 PASS",
    "WARN": "🟡 WARN",
    "BLOCK": "🔴 BLOCK",
}

_POLICY_RESULT_ICONS: dict[str, str] = {
    "PASS": "✅",
    "FAIL": "❌",
    "WARN": "⚠️",
}

_IN_FLIGHT_STATUSES = frozenset({"INGESTED", "DIAGNOSING", "PATCHING", "EXECUTING"})

_TERMINAL_STATUSES = frozenset({"RESOLVED", "REJECTED", "FAILED"})


def status_badge(status: str) -> str:
    return _STATUS_BADGES.get(status, status)


def step_kind_icon(kind: str) -> str:
    return _STEP_KIND_ICONS.get(kind, "•")


def step_status_icon(status: str) -> str:
    return _STEP_STATUS_ICONS.get(status, "•")


def verdict_badge(verdict: str) -> str:
    return _VERDICT_BADGES.get(verdict, verdict)


def policy_result_icon(result: str) -> str:
    return _POLICY_RESULT_ICONS.get(result, "•")


def is_in_flight(status: str) -> bool:
    """Whether the orchestrator is still actively working on the incident --
    used to decide if the UI should keep auto-refreshing."""
    return status in _IN_FLIGHT_STATUSES


def is_terminal(status: str) -> bool:
    return status in _TERMINAL_STATUSES


def latest_audit_verdict(audits: list[dict[str, Any]]) -> str | None:
    """Verdict of the most recently written governance audit, if any."""
    if not audits:
        return None
    return str(audits[-1]["verdict"])


def can_decide(incident_status: str) -> bool:
    """Whether the Approve/Reject footer should be shown at all."""
    return incident_status == "AWAITING_APPROVAL"


def can_execute(incident_status: str, audits: list[dict[str, Any]]) -> bool:
    """Whether the Approve & Execute button should be enabled.

    Mirrors (client-side, best-effort) the same precondition
    `app.agents.executor.execute_incident` re-validates server-side: the
    incident must be awaiting approval and the latest governance verdict
    must not be BLOCK. The server check is authoritative -- this only
    controls whether the button is clickable.
    """
    if not can_decide(incident_status):
        return False
    verdict = latest_audit_verdict(audits)
    # `None` (no audit at all) must NOT be treated as passable -- the
    # orchestrator is supposed to guarantee AWAITING_APPROVAL never
    # happens without one (see app/agents/orchestrator.py's
    # `_validated_finish_status`), but this button shouldn't blindly
    # trust that if the data it can see says otherwise.
    return verdict is not None and verdict != "BLOCK"


def format_timestamp(value: str | datetime | None) -> str:
    if value is None:
        return "-"
    as_datetime: datetime
    if isinstance(value, str):
        try:
            as_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        as_datetime = value
    return as_datetime.strftime("%H:%M:%S.") + f"{as_datetime.microsecond // 1000:03d}"


def format_full_timestamp(value: str | datetime | None) -> str:
    """Like `format_timestamp`, but includes the date -- used by the
    incident history view, which (unlike a single incident's timeline) can
    span many days."""
    if value is None:
        return "-"
    as_datetime: datetime
    if isinstance(value, str):
        try:
            as_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        as_datetime = value
    return as_datetime.strftime("%Y-%m-%d %H:%M:%S UTC")


def incident_status_counts(incidents: list[dict[str, Any]]) -> dict[str, int]:
    """Tally of incidents per status, for the history view's summary
    metrics (e.g. "how many have been resolved so far")."""
    counts: dict[str, int] = {}
    for incident in incidents:
        counts[incident["status"]] = counts.get(incident["status"], 0) + 1
    return counts
