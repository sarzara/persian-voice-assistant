import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.agent import provider as provider_mod
from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.evaluation import NOW
from app.agent.provider import LLMError, OpenAICompatibleProvider
from app.agent.usage import cost_usd, infer_purpose, llm_purpose_var, prices, record_llm_call
from app.db.models import LlmCall
from app.db.session import session_scope
from tests.test_agent import owner  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def nosleep(_):
        return None

    monkeypatch.setattr(provider_mod.asyncio, "sleep", nosleep)
    prices.cache_clear()
    yield
    prices.cache_clear()


def test_cost_uses_cached_and_output_prices():
    # 1M prompt (200k cached) + 100k output on deepseek-v4-flash
    c = cost_usd("deepseek-v4-flash", 1_000_000, 100_000, 200_000)
    assert c == Decimal("0.800000") * Decimal("0.15") + Decimal("0.0006") + Decimal("0.06")
    assert cost_usd("unknown-model", 10, 10) is None


def test_prices_overridable_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PRICES", json.dumps({"my-model": {"input": 1, "output": 2}}))
    from app.config import get_settings

    get_settings.cache_clear()
    prices.cache_clear()
    assert cost_usd("my-model", 1_000_000, 1_000_000) == Decimal("3.000000")
    assert "deepseek-v4-flash" in prices()  # defaults kept


def test_purpose_inferred_from_conversation_shape():
    assert infer_purpose([{"role": "system"}, {"role": "user"}]) == "intent"
    assert infer_purpose([{"role": "system"}, {"role": "user"}, {"role": "assistant"}, {"role": "tool"}]) == "tool_step"
    token = llm_purpose_var.set("review")
    assert infer_purpose([{"role": "user"}]) == "review"
    llm_purpose_var.reset(token)


def body(tool_calls=None, usage=None):
    msg = {"role": "assistant", "content": None if tool_calls else "x"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": "deepseek-v4-flash", "choices": [{"message": msg}],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 600}}


async def test_provider_reports_every_call_including_failures():
    seen = []

    async def logger(rec):
        seen.append(rec)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] in (2, 3, 4, 5) else httpx.Response(200, json=body())

    p = OpenAICompatibleProvider(api_key="k", base_url="https://x", model="deepseek-v4-flash", max_retries=3,
                                 transport=httpx.MockTransport(handler), call_logger=logger)
    await p.complete([{"role": "user", "content": "a"}], [])
    with pytest.raises(LLMError):
        await p.complete([{"role": "user", "content": "a"}, {"role": "assistant", "content": ""}], [])
    ok, err = seen
    assert (ok.status, ok.prompt_tokens, ok.cached_tokens, ok.attempts, ok.purpose) == ("ok", 1000, 600, 1, "intent")
    assert (err.status, err.attempts, err.purpose) == ("error", 4, "tool_step") and "503" in err.error


async def test_agent_run_writes_llm_calls_with_run_id(owner):
    """End to end: real provider class + DB logger; each call is in llm_calls immediately."""
    turns = [
        [{"id": "c1", "type": "function", "function": {"name": "get_current_datetime", "arguments": "{}"}}],
        [{"id": "c2", "type": "function", "function": {"name": "ask_user",
                                                         "arguments": json.dumps({"question": "چه ساعتی؟"})}}],
    ]

    def handler(request):
        return httpx.Response(200, json=body(turns.pop(0)))

    p = OpenAICompatibleProvider(api_key="k", base_url="https://x", model="deepseek-v4-flash",
                                 transport=httpx.MockTransport(handler), call_logger=record_llm_call)
    out = await Agent(p).run(user=owner, message_id=None, transcript="فردا جلسه", context=AgentContext(now=NOW))
    async with session_scope() as s:
        rows = (await s.execute(select(LlmCall).order_by(LlmCall.id))).scalars().all()
    assert [r.purpose for r in rows] == ["intent", "tool_step"]
    assert all(r.run_id == out.run_id and r.status == "ok" and r.cached_tokens == 600 for r in rows)
    assert rows[0].cost_usd == cost_usd("deepseek-v4-flash", 1000, 50, 600)
