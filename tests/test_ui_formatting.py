"""Tests for `ui/formatting.py` -- plain unit tests, no Streamlit runtime
needed since these helpers are kept side-effect free."""

from __future__ import annotations

from ui.formatting import (
    can_decide,
    can_execute,
    format_timestamp,
    is_in_flight,
    is_terminal,
    latest_audit_verdict,
    policy_result_icon,
    status_badge,
    step_kind_icon,
    step_status_icon,
    verdict_badge,
)


def test_status_badge_known_and_unknown() -> None:
    assert "Awaiting" in status_badge("AWAITING_APPROVAL")
    assert status_badge("SOMETHING_NEW") == "SOMETHING_NEW"


def test_step_kind_and_status_icons_fall_back_gracefully() -> None:
    assert step_kind_icon("TOOL_CALL") != "•"
    assert step_kind_icon("unknown_kind") == "•"
    assert step_status_icon("OK") != "•"
    assert step_status_icon("unknown_status") == "•"


def test_verdict_and_policy_icons() -> None:
    assert "BLOCK" in verdict_badge("BLOCK")
    assert verdict_badge("MYSTERY") == "MYSTERY"
    assert policy_result_icon("PASS") != "•"
    assert policy_result_icon("nope") == "•"


def test_is_in_flight_and_is_terminal_partition_statuses() -> None:
    for status in ["INGESTED", "DIAGNOSING", "PATCHING", "EXECUTING"]:
        assert is_in_flight(status)
        assert not is_terminal(status)
    for status in ["RESOLVED", "REJECTED", "FAILED"]:
        assert is_terminal(status)
        assert not is_in_flight(status)
    assert not is_in_flight("AWAITING_APPROVAL")
    assert not is_terminal("AWAITING_APPROVAL")


def test_latest_audit_verdict_uses_most_recent_entry() -> None:
    assert latest_audit_verdict([]) is None
    audits = [{"verdict": "WARN"}, {"verdict": "BLOCK"}]
    assert latest_audit_verdict(audits) == "BLOCK"


def test_can_decide_only_when_awaiting_approval() -> None:
    assert can_decide("AWAITING_APPROVAL")
    assert not can_decide("RESOLVED")
    assert not can_decide("DIAGNOSING")


def test_can_execute_requires_awaiting_approval_and_no_block() -> None:
    assert can_execute("AWAITING_APPROVAL", [{"verdict": "PASS"}])
    assert can_execute("AWAITING_APPROVAL", [{"verdict": "WARN"}])
    assert not can_execute("AWAITING_APPROVAL", [{"verdict": "BLOCK"}])
    assert not can_execute("DIAGNOSING", [{"verdict": "PASS"}])


def test_can_execute_requires_at_least_one_audit() -> None:
    # No audit at all must not be treated as "not BLOCK, so go ahead" --
    # see app/agents/orchestrator.py's `_validated_finish_status`, which
    # this mirrors client-side.
    assert not can_execute("AWAITING_APPROVAL", [])


def test_can_execute_only_considers_latest_verdict() -> None:
    audits = [{"verdict": "BLOCK"}, {"verdict": "PASS"}]
    assert can_execute("AWAITING_APPROVAL", audits)


def test_format_timestamp_handles_none_iso_string_and_datetime() -> None:
    import datetime as dt

    assert format_timestamp(None) == "-"
    assert format_timestamp("not-a-timestamp") == "not-a-timestamp"
    assert format_timestamp("2026-08-23T20:04:37.821662Z") == "20:04:37.821"
    value = dt.datetime(2026, 8, 23, 20, 4, 37, 821000, tzinfo=dt.UTC)
    assert format_timestamp(value) == "20:04:37.821"
