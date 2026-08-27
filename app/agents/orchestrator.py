"""`WardenOrchestrator` -- the multi-turn Gemini tool-calling harness.

See docs/architecture.md section 4 for the authoritative loop spec. This
module owns the conversation with the orchestrator model (Gemini 3 Pro): it
builds the `types.Content` history, calls the model with automatic
function calling disabled, dispatches any function calls it returns to
`app.agents.tool_registry`, feeds the results back as `FunctionResponse`
parts, and stops once the model produces a final no-tool-call message, a
governance tool reports verdict `"BLOCK"`, or the turn/incident budgets run
out.

`WardenOrchestrator.run()` never raises: every failure is captured as
`IncidentState.status = "FAILED"` plus `IncidentState.error`, so a bug (or a
flaky Gemini/BigQuery call) in one incident's reasoning loop can never take
down the orchestrator process or other in-flight incidents.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any, Protocol, cast

from google.genai import types

from app.agents.genai_client import get_genai_client
from app.agents.prompts import SYSTEM_PROMPT
from app.agents.retry import RETRYABLE_EXCEPTIONS, call_with_retry
from app.agents.tool_registry import TOOL_IMPL, TOOLS
from app.config import Settings, get_settings
from app.models.enums import StepActor, StepKind, StepStatus
from app.models.state import AgentStepLog, IncidentState
from app.persistence.base import StateManager, format_step_id
from app.persistence.factory import get_state_manager

_TOOL_ACTORS: dict[str, StepActor] = {
    "investigate_incident_logs": "sub_agent_1_gemma",
    "generate_and_test_patch": "sub_agent_2_flash",
    "verify_governance_policy": "sub_agent_3_flash",
}


def _actor_for_tool(name: str) -> StepActor:
    return _TOOL_ACTORS.get(name, "orchestrator")


def _render_initial_prompt(incident: IncidentState) -> str:
    payload = json.dumps(incident.raw_event, indent=2, default=str, sort_keys=True)
    return (
        "A new incident has been ingested. Investigate it and drive it to a "
        "resolution recommendation using the tools available to you.\n\n"
        f"- incident_id: {incident.incident_id}\n"
        f"- source: {incident.source}\n"
        f"- resource_uri: {incident.resource_uri}\n"
        f"- severity: {incident.severity}\n\n"
        f"Raw event payload:\n```json\n{payload}\n```"
    )


def _extract_model_content(response: types.GenerateContentResponse) -> types.Content:
    if response.candidates and response.candidates[0].content is not None:
        content = response.candidates[0].content
        if content.role is None:
            return content.model_copy(update={"role": "model"})
        return content
    return types.Content(role="model", parts=[types.Part(text=response.text or "")])


class GenAIModelsClient(Protocol):
    """The narrow slice of `genai.Client` the orchestrator depends on, so
    tests can inject a fake without touching real Gemini credentials."""

    aio: Any


class WardenOrchestrator:
    def __init__(
        self,
        *,
        state_manager: StateManager | None = None,
        genai_client: GenAIModelsClient | None = None,
    ) -> None:
        self._state_manager = state_manager or get_state_manager()
        self._genai_client = genai_client

    async def run(self, incident_id: str) -> None:
        settings = get_settings()
        try:
            await asyncio.wait_for(
                self._run_loop(incident_id, settings),
                timeout=settings.warden_incident_timeout_s,
            )
        except TimeoutError:
            await self._fail(incident_id, "incident_timeout_exceeded")
        except Exception as exc:  # noqa: BLE001 - orchestrator.run() must never raise
            await self._fail(incident_id, f"{type(exc).__name__}: {exc}")

    async def _validated_finish_status(self, incident_id: str) -> tuple[str, str | None]:
        """The model produced a final (no-tool-call) turn -- but that alone
        doesn't guarantee there's actually something safe to approve (e.g.
        the model may have given up after a tool error without ever
        producing a patch). `RemediationExecutor` documents that it trusts
        the orchestrator to never let a patch-less/un-audited/BLOCKed
        incident reach `AWAITING_APPROVAL` in the first place -- this is
        what actually enforces that guarantee. Returns `(status, error)`;
        `error` is set (and `status` is `FAILED`/`REJECTED`) whenever the
        model finished without a valid, non-BLOCK-audited patch.
        """
        patches = await self._state_manager.list_patches(incident_id)
        if not patches:
            return "FAILED", "orchestrator_finished_without_patch"

        patch = patches[-1]
        audits = await self._state_manager.list_audits(incident_id)
        matching = [a for a in audits if a.linked_patch_id == patch.patch_id]
        if not matching:
            return "FAILED", "orchestrator_finished_without_governance_audit"
        if matching[-1].verdict == "BLOCK":
            return "REJECTED", None

        return "AWAITING_APPROVAL", None

    async def _fail(self, incident_id: str, error: str) -> None:
        try:
            await self._state_manager.update_incident(incident_id, status="FAILED", error=error)
        except Exception:  # noqa: BLE001 - best effort; incident may not exist at all
            pass

    async def _run_loop(self, incident_id: str, settings: Settings) -> None:
        incident = await self._state_manager.update_incident(incident_id, status="DIAGNOSING")

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=_render_initial_prompt(incident))])
        ]
        next_id = count(1).__next__

        for turn in range(1, settings.warden_max_turns + 1):
            response = await self._call_model_turn(incident_id, contents, next_id, settings)
            contents.append(_extract_model_content(response))

            function_calls = response.function_calls or []
            if not function_calls:
                status, error = await self._validated_finish_status(incident_id)
                await self._state_manager.update_incident(
                    incident_id,
                    status=status,
                    summary=response.text or "",
                    turn_count=turn,
                    error=error,
                )
                return

            blocked = False
            response_parts: list[types.Part] = []
            for call in function_calls:
                tool_result = await self._call_tool(incident_id, call, next_id, settings)
                response_parts.append(
                    types.Part.from_function_response(name=call.name or "", response=tool_result)
                )
                if tool_result.get("verdict") == "BLOCK":
                    blocked = True

            contents.append(types.Content(role="user", parts=response_parts))

            if blocked:
                await self._state_manager.update_incident(
                    incident_id, status="REJECTED", turn_count=turn
                )
                return

            await self._state_manager.update_incident(incident_id, turn_count=turn)

        await self._state_manager.update_incident(
            incident_id,
            status="FAILED",
            error="turn_budget_exhausted",
            turn_count=settings.warden_max_turns,
        )

    async def _append_step(
        self,
        incident_id: str,
        step_sequence: int,
        *,
        kind: StepKind,
        actor: StepActor,
        status: StepStatus,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result_ref: str | None = None,
        content_markdown: str | None = None,
        latency_ms: int | None = None,
        error_detail: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC)
        log = AgentStepLog(
            step_id=format_step_id(step_sequence),
            parent_incident_id=incident_id,
            kind=kind,
            actor=actor,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result_ref=tool_result_ref,
            content_markdown=content_markdown,
            latency_ms=latency_ms,
            status=status,
            error_detail=error_detail,
            started_at=started_at or now,
            finished_at=None if status == "RUNNING" else now,
        )
        await self._state_manager.append_step(incident_id, log)

    async def _generate_content(
        self, contents: list[types.Content], settings: Settings
    ) -> types.GenerateContentResponse:
        client = self._genai_client or get_genai_client()
        return await client.aio.models.generate_content(
            model=settings.warden_orchestrator_model,
            contents=cast(types.ContentListUnion, contents),
            config=types.GenerateContentConfig(
                tools=TOOLS,
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )

    async def _call_model_turn(
        self,
        incident_id: str,
        contents: list[types.Content],
        next_id: Callable[[], int],
        settings: Settings,
    ) -> types.GenerateContentResponse:
        step_sequence = next_id()
        started_at = datetime.now(UTC)
        start = time.monotonic()

        async def _on_retry(exc: BaseException, attempt: int) -> None:
            await self._append_step(
                incident_id,
                step_sequence,
                kind="MODEL_TURN",
                actor="orchestrator",
                status="ERROR",
                error_detail=f"attempt {attempt} failed: {exc}",
                started_at=started_at,
            )

        async def _invoke() -> types.GenerateContentResponse:
            return await asyncio.wait_for(
                self._generate_content(contents, settings),
                timeout=settings.warden_turn_timeout_s,
            )

        try:
            response = await call_with_retry(
                _invoke, on_retry=_on_retry, retry_exceptions=RETRYABLE_EXCEPTIONS
            )
        except TimeoutError:
            await self._append_step(
                incident_id,
                step_sequence,
                kind="MODEL_TURN",
                actor="orchestrator",
                status="TIMEOUT",
                error_detail="model turn exceeded WARDEN_TURN_TIMEOUT_S",
                started_at=started_at,
            )
            raise
        except Exception as exc:
            await self._append_step(
                incident_id,
                step_sequence,
                kind="MODEL_TURN",
                actor="orchestrator",
                status="ERROR",
                error_detail=str(exc),
                started_at=started_at,
            )
            raise

        latency_ms = int((time.monotonic() - start) * 1000)
        function_calls = response.function_calls or []
        content_markdown = (
            "Calling tools: " + ", ".join(c.name or "?" for c in function_calls)
            if function_calls
            else (response.text or "")
        )
        await self._append_step(
            incident_id,
            step_sequence,
            kind="MODEL_TURN",
            actor="orchestrator",
            status="OK",
            content_markdown=content_markdown,
            latency_ms=latency_ms,
            started_at=started_at,
        )
        return response

    async def _call_tool(
        self,
        incident_id: str,
        call: types.FunctionCall,
        next_id: Callable[[], int],
        settings: Settings,
    ) -> dict[str, Any]:
        name = call.name or "unknown_tool"
        args: dict[str, Any] = dict(call.args or {})
        actor = _actor_for_tool(name)

        call_step = next_id()
        started_at = datetime.now(UTC)
        await self._append_step(
            incident_id,
            call_step,
            kind="TOOL_CALL",
            actor=actor,
            status="RUNNING",
            tool_name=name,
            tool_args=args,
            started_at=started_at,
        )

        result_step = next_id()
        start = time.monotonic()
        impl = TOOL_IMPL.get(name)

        if impl is None:
            result: dict[str, Any] = {"error": "UNKNOWN_TOOL", "tool_name": name}
            await self._append_step(
                incident_id,
                result_step,
                kind="TOOL_RESULT",
                actor=actor,
                status="ERROR",
                tool_name=name,
                error_detail=f"no implementation registered for tool {name!r}",
                started_at=started_at,
            )
            return result

        try:
            result = await asyncio.wait_for(
                impl(**{**args, "incident_id": incident_id}),
                timeout=settings.warden_tool_timeout_s,
            )
        except TimeoutError:
            result = {"error": "TIMEOUT", "tool_name": name}
            await self._append_step(
                incident_id,
                result_step,
                kind="TOOL_RESULT",
                actor=actor,
                status="TIMEOUT",
                tool_name=name,
                error_detail="tool call exceeded WARDEN_TOOL_TIMEOUT_S",
                latency_ms=int((time.monotonic() - start) * 1000),
                started_at=started_at,
            )
            return result
        except Exception as exc:  # noqa: BLE001 - a broken tool must not crash the run
            result = {"error": "TOOL_EXCEPTION", "tool_name": name, "detail": str(exc)}
            await self._append_step(
                incident_id,
                result_step,
                kind="TOOL_RESULT",
                actor=actor,
                status="ERROR",
                tool_name=name,
                error_detail=str(exc),
                latency_ms=int((time.monotonic() - start) * 1000),
                started_at=started_at,
            )
            return result

        await self._append_step(
            incident_id,
            result_step,
            kind="TOOL_RESULT",
            actor=actor,
            status="OK",
            tool_name=name,
            tool_result_ref=result.get("firestore_path"),
            content_markdown=json.dumps(result, default=str)[:2000],
            latency_ms=int((time.monotonic() - start) * 1000),
            started_at=started_at,
        )
        return result
