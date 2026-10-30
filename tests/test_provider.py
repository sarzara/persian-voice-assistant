import json

import httpx
import pytest

from app.agent import provider as provider_mod
from app.agent.provider import LLMError, OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(provider_mod.asyncio, "sleep", fake_sleep)
    return slept


def ok_body(tool_calls=None, content=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "model": "deepseek-chat",
        "choices": [{"message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }


def make(handler, **kw):
    return OpenAICompatibleProvider(
        api_key="sk-test", base_url="https://llm.test/", model="deepseek-chat",
        transport=httpx.MockTransport(handler), **kw,
    )


async def test_parses_tool_calls_usage_and_sends_tools():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body([
            {"id": "c1", "type": "function", "function": {"name": "find_contact", "arguments": '{"name": "علی"}'}}
        ]))

    p = make(handler)
    r = await p.complete([{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "find_contact"}}])
    assert seen["url"] == "https://llm.test/chat/completions"
    assert seen["body"]["tool_choice"] == "auto" and seen["body"]["temperature"] == 0.0
    assert r.tool_calls[0].name == "find_contact" and r.tool_calls[0].parsed_arguments() == {"name": "علی"}
    assert (r.prompt_tokens, r.completion_tokens) == (120, 30)
    assert r.message["role"] == "assistant" and r.message["tool_calls"]


async def test_retries_429_then_succeeds_honouring_retry_after(_no_sleep):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=ok_body(content="سلام"))

    r = await make(handler).complete([], [])
    assert r.content == "سلام" and calls["n"] == 2 and _no_sleep == [2.0]


async def test_timeouts_retried_then_raise():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as err:
        await make(handler, max_retries=2).complete([], [])
    assert calls["n"] == 3 and err.value.retryable


@pytest.mark.parametrize("status", [400, 401, 402, 422])
async def test_client_errors_not_retried(status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(LLMError) as err:
        await make(handler).complete([], [])
    assert calls["n"] == 1 and err.value.status_code == status


async def test_extra_body_merged_into_request():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body(content="x"))

    await make(handler, extra_body={"thinking": {"type": "disabled"}}).complete([], [])
    assert seen["thinking"] == {"type": "disabled"}


async def test_malformed_response_raises():
    with pytest.raises(LLMError):
        await make(lambda r: httpx.Response(200, json={"choices": []})).complete([], [])


def test_missing_key_rejected():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(api_key="", base_url="https://x", model="m")


async def test_reasoning_content_kept_in_history_for_tool_loops():
    def handler(request):
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"message": {
            "role": "assistant", "content": "", "reasoning_content": "think...",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}}]})

    r = await make(handler).complete([], [])
    assert r.message["reasoning_content"] == "think..." and r.message["tool_calls"]


async def test_no_reasoning_field_added_when_absent():
    r = await make(lambda req: httpx.Response(200, json=ok_body(content="x"))).complete([], [])
    assert "reasoning_content" not in r.message
