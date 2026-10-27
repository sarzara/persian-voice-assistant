"""Outbound configuration for restricted networks: gateway URLs and env proxies."""

import httpx
import pytest

from app.api import health
from app.tools.resend_client import ResendClient


@pytest.fixture(autouse=True)
def _clear():
    from app.config import get_settings

    get_settings.cache_clear()
    health._cache.clear()
    yield
    get_settings.cache_clear()


def test_bot_uses_configured_telegram_gateway(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("TELEGRAM_API_BASE_URL", "https://tg-gw.example.org/")
    from app.bot.main import build_application

    bot = build_application().bot
    assert bot.base_url == "https://tg-gw.example.org/bot123:abc"
    assert bot.base_file_url == "https://tg-gw.example.org/file/bot123:abc"


async def test_health_probes_the_gateway(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_BASE_URL", "https://tg-gw.example.org")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"result": {"username": "b"}})

    await health._telegram(httpx.MockTransport(handler))
    assert seen == ["https://tg-gw.example.org/bot123:abc/getMe"]


async def test_resend_base_url_configurable(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("RESEND_API_URL", "https://mail-gw.example.org")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "m1"})

    async with ResendClient.from_settings(transport=httpx.MockTransport(handler)) as c:
        await c.send_email("b@example.com", "s", text="t")
    assert seen == ["https://mail-gw.example.org/emails"]


def test_socks_proxies_are_supported(monkeypatch):
    """httpx needs the socks extra for socks5:// proxies; constructing a client must not fail."""
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    client = httpx.AsyncClient()  # reads HTTPS_PROXY (trust_env)
    assert client is not None
