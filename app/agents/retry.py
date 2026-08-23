"""Async retry helper built on `tenacity` (see docs/architecture.md section 4).

Retries transient failures from Gemini calls with exponential backoff, and
invokes an optional async hook before each retry so the orchestrator can
persist an `AgentStepLog(status="ERROR")` entry before the next attempt.

The architecture doc's retry list names `aiohttp.ClientError`, but this
project's HTTP dependency is `httpx` (no `aiohttp` is installed anywhere in
`pyproject.toml`), so `httpx.HTTPError` is the real-world equivalent here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import tenacity
from google.api_core.exceptions import DeadlineExceeded, ResourceExhausted, ServiceUnavailable

T = TypeVar("T")

RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ServiceUnavailable,
    DeadlineExceeded,
    ResourceExhausted,
    httpx.HTTPError,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 8.0

OnRetryHook = Callable[[BaseException, int], Awaitable[None]]


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    on_retry: OnRetryHook | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_exceptions: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
) -> T:
    """Call `fn()`, retrying on `retry_exceptions` with exponential backoff.

    Exceptions not in `retry_exceptions` (e.g. a per-turn `TimeoutError`)
    propagate immediately on the first attempt -- they are deliberately not
    retried here, per the timeout matrix in docs/architecture.md section 4.

    `on_retry(exc, attempt_number)` is awaited before each retry's backoff
    sleep, so callers can persist an `AgentStepLog(status="ERROR")` entry
    before the next attempt runs.
    """

    async def _before_sleep(retry_state: tenacity.RetryCallState) -> None:
        if on_retry is None or retry_state.outcome is None:
            return
        exc = retry_state.outcome.exception()
        if exc is not None:
            await on_retry(exc, retry_state.attempt_number)

    retrying = tenacity.AsyncRetrying(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(
            multiplier=DEFAULT_BACKOFF_BASE_S, max=DEFAULT_BACKOFF_MAX_S
        ),
        retry=tenacity.retry_if_exception_type(retry_exceptions),
        before_sleep=_before_sleep,
        reraise=True,
    )
    return await retrying(fn)
