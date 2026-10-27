import json
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from telegram.ext import ApplicationHandlerStop

from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.evaluation import NOW, SAMPLES
from app.bot import handlers
from app.bot import messages as msg
from app.bot.ratelimit import RateLimiter
from app.db.models import User, VoiceMessage
from app.db.session import session_scope
from app.logging_setup import ContextFilter, JsonFormatter, request_id_var, set_run_id
from app.stt.cleanup import cleanup_audio
from tests.fakes import ScriptedProvider
from tests.test_agent import owner  # noqa: F401  (fixture)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.addFilter(ContextFilter())
        self.setFormatter(JsonFormatter("test"))
        self.lines: list[dict] = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def logs():
    cap = Capture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(cap)
    root.setLevel(logging.INFO)
    yield cap.lines
    root.removeHandler(cap)
    root.setLevel(old)


# ------------------------------------------------------------- logging ---

def test_json_lines_carry_context_and_extras(logs):
    request_id_var.set("tg-77")
    set_run_id(5)
    logging.getLogger("x").info("hello %s", "world", extra={"tool": "find_contact"})
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("x").exception("failed")
    first, second = logs[-2], logs[-1]
    assert first["msg"] == "hello world" and first["request_id"] == "tg-77" and first["run_id"] == 5
    assert first["tool"] == "find_contact" and first["service"] == "test" and first["level"] == "INFO"
    assert "ValueError: boom" in second["exc"]


async def test_run_id_flows_through_agent_and_tools(owner, logs):
    request_id_var.set("tg-1")
    set_run_id(None)
    sample = SAMPLES[0]
    out = await Agent(ScriptedProvider(sample.script)).run(user=owner, message_id=None, transcript=sample.transcript,
                                                           context=AgentContext(now=NOW))
    mine = [l for l in logs if l["logger"].startswith(("app.agent", "app.tools"))]
    assert any(l["logger"] == "app.tools.registry" for l in mine)
    assert mine and all(l["run_id"] == out.run_id and l["request_id"] == "tg-1" for l in mine)


async def test_api_echoes_request_id_and_hides_details(monkeypatch, db):
    monkeypatch.setenv("ADMIN_API_KEY", "k" * 20)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.api import admin
    from app.api.main import create_app

    async def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(admin, "run_detail", boom)
    async with AsyncClient(transport=ASGITransport(app=create_app(), raise_app_exceptions=False), base_url="http://t") as c:
        ok = await c.get("/health/live", headers={"X-Request-ID": "abc-123"})
        bad = await c.get("/health/live", headers={"X-Request-ID": "x\ny"})
        err = await c.get("/runs/1", headers={"X-Admin-Key": "k" * 20})
    assert ok.headers["x-request-id"] == "abc-123"
    assert bad.headers["x-request-id"].startswith("api-")  # unsafe header value replaced
    assert err.status_code == 500 and err.json()["detail"] == "internal error" and err.json()["request_id"]
    assert "secret detail" not in err.text


# ----------------------------------------------------------- rate limit ---

def test_rate_limiter_minute_and_hour_windows():
    t = [0.0]
    rl = RateLimiter(per_minute=3, per_hour=5, clock=lambda: t[0])
    assert [rl.check(1) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert rl.check(1) > 0            # 4th in the same minute
    assert rl.check(2) == 0.0         # other users unaffected
    t[0] = 61
    assert rl.check(1) == 0.0 and rl.check(1) == 0.0
    t[0] = 125
    assert rl.check(1) > 60           # hourly cap (5) reached
    t[0] = 3601
    assert rl.check(1) == 0.0


def make_update(user_id=111111111, callback=False):
    message = SimpleNamespace(reply_text=AsyncMock(), text="hi")
    return SimpleNamespace(
        update_id=9, effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type="private"),
        effective_message=message, callback_query=SimpleNamespace(data="run:1:confirm") if callback else None,
    )


async def test_gate_rate_limits_messages_but_not_button_taps(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111111111")
    monkeypatch.setattr(handlers, "_rate_limiter", RateLimiter(per_minute=2, per_hour=100))
    for _ in range(2):
        await handlers.allowlist_gate(make_update(), None)
    limited = make_update()
    with pytest.raises(ApplicationHandlerStop):
        await handlers.allowlist_gate(limited, None)
    assert "دقیقه" in limited.effective_message.reply_text.await_args.args[0]
    for _ in range(5):
        await handlers.allowlist_gate(make_update(callback=True), None)  # Confirm always works
    assert request_id_var.get() == "tg-9"


async def test_voice_over_60_seconds_refused(monkeypatch):
    voice = SimpleNamespace(file_size=1000, duration=61, get_file=AsyncMock())
    update = SimpleNamespace(effective_message=SimpleNamespace(voice=voice, reply_text=AsyncMock(), message_id=1),
                             effective_user=SimpleNamespace(id=111111111, full_name="x"))
    await handlers.voice(update, None)
    voice.get_file.assert_not_called()
    assert "۶۰" in update.effective_message.reply_text.await_args.args[0]


async def test_unhandled_error_reply_is_short_persian_with_reference():
    from telegram import Update

    request_id_var.set("tg-4242")
    reply = AsyncMock()
    upd = Update(update_id=4242)
    ctx = SimpleNamespace(error=RuntimeError("DB password=xyz"))
    import unittest.mock as um

    with um.patch.object(Update, "effective_message", new=SimpleNamespace(reply_text=reply)):
        await handlers.on_error(upd, ctx)
    text = reply.await_args.args[0]
    assert "کد پیگیری: tg-4242" in text and "xyz" not in text and len(text) < 200
    await handlers.on_error(object(), ctx)  # non-Update input must not raise


# -------------------------------------------------------------- cleanup ---

async def test_cleanup_deletes_only_old_audio_and_clears_db(db, tmp_path):
    old, fresh, other = tmp_path / "1" / "old.ogg", tmp_path / "1" / "new.wav", tmp_path / "1" / "notes.txt"
    old.parent.mkdir()
    for f in (old, fresh, other):
        f.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(other, (two_hours_ago, two_hours_ago))
    async with session_scope() as s:
        u = User(telegram_user_id=5)
        s.add(u)
        await s.flush()
        s.add(VoiceMessage(user_id=u.id, telegram_file_id="f", audio_path=str(old)))

    removed = await cleanup_audio(tmp_path, max_age_s=3600)

    assert removed == [old] and not old.exists() and fresh.exists() and other.exists()
    async with session_scope() as s:
        assert (await s.execute(select(VoiceMessage.audio_path))).scalar_one() is None
