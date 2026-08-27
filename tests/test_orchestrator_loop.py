"""Tests for `WardenOrchestrator`'s multi-turn tool-calling loop.

No real Gemini credentials or GCP resources are needed here: a `FakeModels`
stub stands in for `genai.Client().aio.models`, scripted with a fixed
sequence of `types.GenerateContentResponse` objects (or exceptions) to
return on each call. This exercises the full loop -- turn sequencing, tool
dispatch, retry-on-transient-error, timeouts, governance BLOCK short-circuit,
and turn-budget exhaustion -- fully offline and deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.genai import types

from app.agents.orchestrator import WardenOrchestrator
from app.agents.tool_registry import TOOL_IMPL
from app.config import get_settings
from app.models import IncidentState, new_id
from app.persistence import InMemoryStateManager
from app.persistence.factory import get_state_manager


def _text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))
        ]
    )


def _tool_call_response(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ]
    )


_FINAL_MARKDOWN = (
    "## Root Cause\nn/a\n## Proposed Fix\nn/a\n## Risk\nn/a\n## Recommended Action\nn/a"
)


def _last_function_response(contents: list[types.Content]) -> dict[str, Any]:
    """Read the most recent `FunctionResponse` payload out of the running
    conversation -- lets a scripted turn react to a *real* tool result
    (e.g. the patch_id a previous turn's tool call actually generated)
    instead of a value invented ahead of time."""
    for part in reversed(contents[-1].parts or []):
        if part.function_response is not None:
            return dict(part.function_response.response or {})
    raise AssertionError("expected a function_response in the last content")


class FakeModels:
    """Stand-in for `genai.Client().aio.models`.

    `scripted` items are consumed in order across calls. Each item is one
    of: a `GenerateContentResponse` to return, an `Exception` to raise, or a
    callable `(contents) -> GenerateContentResponse` for turns that need to
    react to a real tool result from earlier in the conversation.
    """

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted: Iterator[Any] = iter(scripted)
        self.call_count = 0
        self.calls: list[list[types.Content]] = []

    async def generate_content(self, **kwargs: Any) -> types.GenerateContentResponse:
        self.call_count += 1
        contents = kwargs["contents"]
        self.calls.append(contents)
        try:
            item = next(self._scripted)
        except StopIteration as exc:
            raise AssertionError("FakeModels ran out of scripted responses") from exc
        if callable(item):
            item = item(contents)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, types.GenerateContentResponse)
        return item


class FakeAio:
    def __init__(self, models: FakeModels) -> None:
        self.models = models


class FakeGenAIClient:
    def __init__(self, scripted: list[Any]) -> None:
        self.models = FakeModels(scripted)
        self.aio = FakeAio(self.models)


def _new_incident(raw_event: dict[str, Any] | None = None) -> IncidentState:
    return IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event=raw_event
        or {"scenario": "schema_drift", "table": "orders", "dropped_column": "email"},
        orchestrator_model="gemini-3.1-pro-preview",
    )


async def _create_incident(state_manager: InMemoryStateManager, incident: IncidentState) -> None:
    await state_manager.create_incident(incident)


async def test_happy_path_runs_all_three_tools_then_awaits_approval() -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    def _governance_turn(contents: list[types.Content]) -> types.GenerateContentResponse:
        patch_result = _last_function_response(contents)
        return _tool_call_response(
            "verify_governance_policy",
            {"patch_id": patch_result["patch_id"], "dataset_id": "proj.ds"},
        )

    fake_client = FakeGenAIClient(
        [
            _tool_call_response(
                "investigate_incident_logs", {"resource_uri": incident.resource_uri}
            ),
            _tool_call_response(
                "generate_and_test_patch",
                {
                    "finding_id": "placeholder",
                    "target_resource_uri": incident.resource_uri,
                    "drift_summary": "Column `email` appears to have been dropped from `orders`.",
                },
            ),
            _governance_turn,
            _text_response(
                "## Root Cause\n...\n## Proposed Fix\n...\n## Risk\n...\n"
                "## Recommended Action\n..."
            ),
        ]
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    assert final.status == "AWAITING_APPROVAL"
    assert final.turn_count == 4
    assert "Root Cause" in (final.summary or "")

    assert len(await state_manager.list_findings(incident.incident_id)) == 1
    assert len(await state_manager.list_patches(incident.incident_id)) == 1
    assert len(await state_manager.list_audits(incident.incident_id)) == 1

    steps = await state_manager.list_steps(incident.incident_id)
    kinds = [s.kind for s in steps]
    assert kinds.count("MODEL_TURN") == 4
    assert kinds.count("TOOL_CALL") == 3
    assert kinds.count("TOOL_RESULT") == 3
    assert all(s.status in ("OK", "RUNNING") for s in steps)

    # incident_id is always injected by the orchestrator itself, overriding
    # whatever the model supplied (or omitted).
    assert fake_client.models.call_count == 4


async def test_governance_block_verdict_rejects_incident_and_stops_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    async def _blocking_governance_check(**kwargs: Any) -> dict[str, Any]:
        return {
            "audit_id": new_id(),
            "verdict": "BLOCK",
            "requires_human_approval": True,
            "policy_checks_passed": 0,
            "policy_checks_failed": 1,
            "pii_columns_touched": ["email"],
            "rationale": "Blocked: dropping a PII-tagged column.",
            "firestore_path": "incidents/x/audits/y",
        }

    monkeypatch.setitem(TOOL_IMPL, "verify_governance_policy", _blocking_governance_check)

    fake_client = FakeGenAIClient(
        [
            _tool_call_response(
                "verify_governance_policy", {"patch_id": "p1", "dataset_id": "proj.ds"}
            )
        ]
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    assert final.status == "REJECTED"
    assert final.turn_count == 1
    # Only one model call was made -- the loop stopped after the BLOCK verdict
    # instead of asking the model for a follow-up turn.
    assert fake_client.models.call_count == 1


async def test_turn_budget_exhaustion_marks_incident_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_MAX_TURNS", "2")
    get_settings.cache_clear()
    try:
        state_manager = get_state_manager()
        assert isinstance(state_manager, InMemoryStateManager)
        incident = _new_incident()
        await _create_incident(state_manager, incident)

        fake_client = FakeGenAIClient(
            [
                _tool_call_response(
                    "investigate_incident_logs", {"resource_uri": incident.resource_uri}
                ),
                _tool_call_response(
                    "investigate_incident_logs", {"resource_uri": incident.resource_uri}
                ),
            ]
        )

        orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
        await orchestrator.run(incident.incident_id)

        final = await state_manager.get_incident(incident.incident_id)
        assert final.status == "FAILED"
        assert final.error == "turn_budget_exhausted"
        assert final.turn_count == 2
        assert fake_client.models.call_count == 2
    finally:
        get_settings.cache_clear()


async def test_transient_model_error_is_retried_and_logs_error_step() -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    fake_client = FakeGenAIClient(
        [
            ServiceUnavailable("temporary blip"),  # type: ignore[no-untyped-call]
            _text_response(_FINAL_MARKDOWN),
        ]
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    # The model finished without ever calling a tool, so there's no patch
    # to approve -- FAILED (not AWAITING_APPROVAL) is correct here; this
    # test's real focus is the retry behavior asserted below.
    assert final.status == "FAILED"
    assert final.error == "orchestrator_finished_without_patch"
    assert fake_client.models.call_count == 2

    steps = await state_manager.list_steps(incident.incident_id)
    model_turn_steps = [s for s in steps if s.kind == "MODEL_TURN"]
    assert [s.status for s in model_turn_steps] == ["ERROR", "OK"]
    assert "temporary blip" in (model_turn_steps[0].error_detail or "")


async def test_model_turn_timeout_fails_incident_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_TURN_TIMEOUT_S", "0")
    get_settings.cache_clear()
    try:
        state_manager = get_state_manager()
        assert isinstance(state_manager, InMemoryStateManager)
        incident = _new_incident()
        await _create_incident(state_manager, incident)

        class _SlowModels(FakeModels):
            async def generate_content(self, **kwargs: Any) -> types.GenerateContentResponse:
                self.call_count += 1
                await asyncio.sleep(1)
                return _text_response("too slow")

        fake_client = FakeGenAIClient([])
        fake_client.models = _SlowModels([])
        fake_client.aio.models = fake_client.models

        orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
        await orchestrator.run(incident.incident_id)

        final = await state_manager.get_incident(incident.incident_id)
        assert final.status == "FAILED"
        assert final.error is not None
        assert "timeout" in final.error.lower()
        # A per-turn timeout is not in the retryable exception set, so the
        # model must never be called a second time for this turn.
        assert fake_client.models.call_count <= 1

        steps = await state_manager.list_steps(incident.incident_id)
        assert [s.status for s in steps if s.kind == "MODEL_TURN"] == ["TIMEOUT"]
    finally:
        get_settings.cache_clear()


async def test_finishing_without_any_patch_fails_the_incident() -> None:
    """Reproduces the real bug hit in production: the model gives up (e.g.
    after a tool error) without ever producing a patch. The incident must
    not reach AWAITING_APPROVAL with nothing to approve."""
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    fake_client = FakeGenAIClient([_text_response(_FINAL_MARKDOWN)])

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    assert final.status == "FAILED"
    assert final.error == "orchestrator_finished_without_patch"


async def test_finishing_with_patch_but_no_audit_fails_the_incident() -> None:
    """A patch alone is not enough -- execution must never be offered
    without a governance audit having actually run against it (mirrors
    `RemediationExecutor`'s own defense-in-depth check)."""
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    fake_client = FakeGenAIClient(
        [
            _tool_call_response(
                "generate_and_test_patch",
                {
                    "finding_id": "placeholder",
                    "target_resource_uri": incident.resource_uri,
                    "drift_summary": "Column `email` appears to have been dropped.",
                },
            ),
            _text_response(_FINAL_MARKDOWN),
        ]
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    assert len(await state_manager.list_patches(incident.incident_id)) == 1
    assert final.status == "FAILED"
    assert final.error == "orchestrator_finished_without_governance_audit"


async def test_validated_finish_status_rejects_when_latest_audit_is_block() -> None:
    """Direct unit test of `_validated_finish_status`'s third branch. The
    `blocked` short-circuit in `_run_loop` already catches a BLOCK verdict
    the moment it's produced (see
    `test_governance_block_verdict_rejects_incident_and_stops_early`), so
    this branch is a defense-in-depth backstop for state seeded any other
    way -- exercised directly here rather than fighting that short-circuit
    through the full loop."""
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    from app.models import GovernanceAudit, SQLPatchPayload

    patch = SQLPatchPayload(
        patch_id=new_id(),
        linked_finding_id="placeholder",
        patch_kind="DDL",
        sandbox_sql="SELECT 1",
        production_sql="SELECT 1",
        validation_status="SANDBOX_PASS",
        patcher_model="test",
    )
    await state_manager.write_patch(incident.incident_id, patch)
    await state_manager.write_audit(
        incident.incident_id,
        GovernanceAudit(
            audit_id=new_id(),
            linked_patch_id=patch.patch_id,
            verdict="BLOCK",
            rationale="Blocked: dropping a PII-tagged column.",
        ),
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=FakeGenAIClient([]))
    status, error = await orchestrator._validated_finish_status(incident.incident_id)
    assert status == "REJECTED"
    assert error is None


async def test_unknown_tool_call_is_reported_as_error_and_loop_continues() -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)
    incident = _new_incident()
    await _create_incident(state_manager, incident)

    fake_client = FakeGenAIClient(
        [
            _tool_call_response("delete_entire_dataset", {"dataset_id": "proj.ds"}),
            _text_response(_FINAL_MARKDOWN),
        ]
    )

    orchestrator = WardenOrchestrator(state_manager=state_manager, genai_client=fake_client)
    await orchestrator.run(incident.incident_id)

    final = await state_manager.get_incident(incident.incident_id)
    # No patch was ever produced (the only tool call was unknown), so the
    # model's final text turn correctly fails rather than awaiting
    # approval on nothing.
    assert final.status == "FAILED"
    assert final.error == "orchestrator_finished_without_patch"

    steps = await state_manager.list_steps(incident.incident_id)
    tool_result_steps = [s for s in steps if s.kind == "TOOL_RESULT"]
    assert len(tool_result_steps) == 1
    assert tool_result_steps[0].status == "ERROR"
    assert "no implementation" in (tool_result_steps[0].error_detail or "")
