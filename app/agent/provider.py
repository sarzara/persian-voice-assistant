"""LLM provider interface and an OpenAI-compatible implementation (used for DeepSeek).

The agent depends only on ``LLMProvider``; swapping DeepSeek for another
OpenAI-compatible endpoint (or a fake in tests) needs no agent changes.

Retries: chat completions have no side effects, so retrying is always safe.
429, 5xx, timeouts and connection errors are retried with exponential backoff
plus jitter, honouring Retry-After. 4xx errors (bad key, no balance, bad
request) fail immediately: retrying cannot fix them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from app.agent.usage import CallRecord, infer_purpose, record_llm_call
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 20.0


class LLMError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # raw JSON string, exactly as the model produced it

    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest]
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    # The assistant message to append to the conversation for the next step.
    message: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        call_logger: Callable[[CallRecord], Awaitable[None]] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("LLM API key is not set")
        self.model = model
        self._max_retries = max_retries
        self._temperature = temperature
        self._extra_body = extra_body or {}
        # Called once per complete() with tokens, latency and status (writes llm_calls in production).
        self._call_logger = call_logger
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        started = time.perf_counter()
        purpose = infer_purpose(messages)
        try:
            response = await self._complete(messages, tools, counter := [0])
        except LLMError as exc:
            await self._log(CallRecord(self.model, purpose, 0, 0, 0, int((time.perf_counter() - started) * 1000),
                                       counter[0], "error", str(exc)[:500]))
            raise
        await self._log(CallRecord(response.model, purpose, response.prompt_tokens, response.completion_tokens,
                                   response.cached_tokens, response.latency_ms, counter[0], "ok"))
        return response

    async def _log(self, rec: CallRecord) -> None:
        if self._call_logger is not None:
            await self._call_logger(rec)

    async def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], counter: list[int]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(self._extra_body)

        started = time.perf_counter()
        last_error: LLMError | None = None
        for attempt in range(self._max_retries + 1):
            counter[0] = attempt + 1
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LLMError(f"LLM request failed: {exc.__class__.__name__}", retryable=True)
                retry_after = None
            else:
                if response.is_success:
                    return self._parse(response.json(), started)
                retryable = response.status_code in RETRYABLE_STATUS
                last_error = LLMError(
                    f"LLM HTTP {response.status_code}: {response.text[:300]}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
                if not retryable:
                    raise last_error
                retry_after = _retry_after_seconds(response)

            if attempt < self._max_retries:
                delay = retry_after if retry_after is not None else _backoff(attempt)
                log.warning("LLM call failed (%s); retry %d/%d in %.1fs", last_error, attempt + 1, self._max_retries, delay)
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _parse(self, body: dict[str, Any], started: float) -> LLMResponse:
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed LLM response: {str(body)[:200]}") from exc

        tool_calls = [
            ToolCallRequest(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"].get("arguments") or "{}")
            for tc in message.get("tool_calls") or []
        ]
        usage = body.get("usage") or {}
        # DeepSeek reports prompt_cache_hit_tokens; OpenAI-style APIs use prompt_tokens_details.cached_tokens.
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        # Keep only the fields the API accepts back in the history.
        history_msg: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            history_msg["tool_calls"] = message["tool_calls"]
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            model=body.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(cached or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=choice.get("finish_reason"),
            message=history_msg,
        )


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_S, 1.0 * 2**attempt) + random.uniform(0, 0.5)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(MAX_BACKOFF_S, max(0.0, float(value)))
    except ValueError:
        return None


def provider_from_settings(settings: Settings | None = None, **kwargs: Any) -> OpenAICompatibleProvider:
    s = settings or get_settings()
    return OpenAICompatibleProvider(
        api_key=s.deepseek_api_key.get_secret_value(),
        base_url=s.deepseek_base_url,
        model=s.deepseek_model,
        timeout_s=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        extra_body=json.loads(s.deepseek_extra_body or "{}"),
        call_logger=kwargs.pop("call_logger", record_llm_call),
        **kwargs,
    )
