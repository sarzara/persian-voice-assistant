"""Readiness: database, Telegram and the LLM provider.

External checks are cached for HEALTH_CACHE_S so frequent probes don't hammer
Telegram/DeepSeek. Liveness (/health/live) checks nothing external and is what
the container healthcheck uses: a Telegram outage must not restart the API.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text

from app.api.schemas import Check
from app.config import get_settings
from app.db.session import get_engine

HEALTH_CACHE_S = 30.0
TIMEOUT_S = 5.0
_cache: dict[str, tuple[float, Check]] = {}


async def _timed(coro) -> Check:
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(coro, timeout=TIMEOUT_S)
        return Check(ok=True, latency_ms=int((time.perf_counter() - started) * 1000), detail=detail)
    except Exception as exc:  # noqa: BLE001 - report, never raise from a probe
        # Class name only (messages may contain URLs/tokens), except our own safe LookupError.
        detail = str(exc) if isinstance(exc, LookupError) else exc.__class__.__name__
        return Check(ok=False, latency_ms=int((time.perf_counter() - started) * 1000), detail=detail)


async def _db() -> str | None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return None


async def _telegram(transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    token = get_settings().telegram_bot_token.get_secret_value()
    if not token:
        raise RuntimeError("not configured")
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as c:
        r = await c.get(f"{get_settings().telegram_api_base_url.rstrip('/')}/bot{token}/getMe")
    r.raise_for_status()
    return "@" + r.json()["result"]["username"]


async def _llm(transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    s = get_settings()
    key = s.deepseek_api_key.get_secret_value()
    if not key:
        raise RuntimeError("not configured")
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as c:
        r = await c.get(s.deepseek_base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    available = {m.get("id") for m in r.json().get("data", [])}
    if available and s.deepseek_model not in available:
        # e.g. a retired model name: the provider is up, but every agent call would fail.
        raise LookupError(f"model {s.deepseek_model!r} not offered; available: {sorted(available)}")
    return s.deepseek_model


async def _cached(name: str, factory) -> Check:
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < HEALTH_CACHE_S:
        return hit[1]
    result = await _timed(factory())
    _cache[name] = (time.monotonic(), result)
    return result


async def readiness(transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Check]:
    db, tg, llm = await asyncio.gather(
        _timed(_db()),  # never cached: the database is local and cheap
        _cached("telegram", lambda: _telegram(transport)),
        _cached("llm", lambda: _llm(transport)),
    )
    return {"database": db, "telegram": tg, "llm": llm}
