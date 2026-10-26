import json

import httpx
import pytest

from app.tools import resend_client
from app.tools.resend_client import ResendClient, ResendError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(resend_client.asyncio, "sleep", instant)


def make_client(handler, **kw):
    return ResendClient("re_test", "Bot <bot@example.com>", transport=httpx.MockTransport(handler), **kw)


async def test_send_success_payload_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["idem"] = request.headers.get("idempotency-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg_123"})

    async with make_client(handler) as client:
        sent = await client.send_email("a@example.com", "سلام", text="متن", idempotency_key="k1")

    assert sent.id == "msg_123"
    assert seen["auth"] == "Bearer re_test"
    assert seen["idem"] == "k1"
    assert seen["body"] == {"from": "Bot <bot@example.com>", "to": ["a@example.com"], "subject": "سلام", "text": "متن"}


async def test_api_error_raised_with_message():
    def handler(request):
        return httpx.Response(422, json={"statusCode": 422, "name": "validation_error", "message": "Invalid `to`"})

    async with make_client(handler) as client:
        with pytest.raises(ResendError) as err:
            await client.send_email("bad", "s", text="t")
    assert err.value.status_code == 422 and err.value.name == "validation_error"


async def test_retries_5xx_when_idempotency_key_given():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"id": "ok"})

    async with make_client(handler) as client:
        assert (await client.send_email("a@example.com", "s", text="t", idempotency_key="k")).id == "ok"
    assert calls["n"] == 3


async def test_no_retry_without_idempotency_key():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    async with make_client(handler) as client:
        with pytest.raises(ResendError):
            await client.send_email("a@example.com", "s", text="t")
    assert calls["n"] == 1


def test_missing_config_rejected():
    with pytest.raises(ValueError):
        ResendClient("", "x@example.com")
