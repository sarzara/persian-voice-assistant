"""LLM usage accounting: prices, cost, and the llm_calls row written for every provider call.

Prices are USD per 1M tokens and come from LLM_PRICES (JSON) so they can follow the
provider's price list without a code change. Defaults: DeepSeek V4 off-peak list prices
as published in October 2026; DeepSeek doubles them in weekday peak hours, so treat the
dashboard's cost as an estimate and keep LLM_PRICES current.
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek-flash": {"input": 0.15, "cached_input": 0.003, "output": 0.60},
    "deepseek-v4-flash": {"input": 0.15, "cached_input": 0.003, "output": 0.60},  # former name
    "deepseek-v4-pro": {"input": 0.66, "cached_input": 0.022, "output": 1.98},
}

# Callers that are not the agent loop (e.g. a future transcript fixer) can label their calls.
llm_purpose_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_purpose", default=None)


@lru_cache
def prices() -> dict[str, dict[str, float]]:
    raw = get_settings().llm_prices.strip()
    configured = json.loads(raw) if raw else {}
    return {**DEFAULT_PRICES, **configured}


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> Decimal | None:
    price = prices().get(model)
    if price is None:
        return None
    cached = min(cached_tokens, prompt_tokens)
    total = (Decimal(prompt_tokens - cached) * Decimal(str(price["input"]))
             + Decimal(cached) * Decimal(str(price.get("cached_input", price["input"])))
             + Decimal(completion_tokens) * Decimal(str(price["output"])))
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def infer_purpose(messages: list[dict[str, Any]]) -> str:
    """The agent's first call has only [system, user]; every later call follows tool results."""
    explicit = llm_purpose_var.get()
    if explicit:
        return explicit
    return "intent" if not any(m.get("role") in ("assistant", "tool") for m in messages) else "tool_step"


@dataclass
class CallRecord:
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int
    attempts: int
    status: str
    error: str | None = None


async def record_llm_call(rec: CallRecord) -> None:
    """Persist one call. Never raises: accounting must not break the user's request."""
    from app.db.models import LlmCall
    from app.db.session import session_scope
    from app.logging_setup import run_id_var

    try:
        async with session_scope() as s:
            s.add(LlmCall(
                run_id=run_id_var.get(), model=rec.model, purpose=rec.purpose,
                prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens,
                cached_tokens=rec.cached_tokens,
                cost_usd=cost_usd(rec.model, rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens),
                latency_ms=rec.latency_ms, attempts=rec.attempts, status=rec.status, error=rec.error,
            ))
    except Exception:  # noqa: BLE001
        log.exception("Could not record llm_call")
        return
    try:
        from app.budget import after_llm_call

        await after_llm_call()
    except Exception:  # noqa: BLE001
        log.exception("Budget check failed")
