"""Dashboard: access control, aggregation queries and pages."""

import datetime as dt
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.dashboard import auth, queries
from app.db.models import AgentRun, LlmCall, RunFeedback, ToolCall, User, VoiceMessage
from app.db.session import session_scope

TEHRAN = ZoneInfo("Asia/Tehran")
NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=TEHRAN)
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password(PASSWORD, iterations=1000))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key-0123456789")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111111111")
    monkeypatch.setenv("DASHBOARD_DB_PASSWORD", "")  # tests read through the main role (read-only transactions)
    from app.config import get_settings
    from app.dashboard import app as dash

    get_settings.cache_clear()
    dash.engine.cache_clear()
    auth.used_tokens._seen.clear()
    auth.throttle._fails.clear()


def client() -> AsyncClient:
    from app.dashboard.app import create_app

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://dash.test")


async def login(c: AsyncClient) -> None:
    page = await c.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = await c.post("/login", data={"password": PASSWORD, "csrf": csrf, "next": "/"})
    assert r.status_code == 303 and auth.COOKIE in r.cookies


# --------------------------------------------------------------- access ---

@pytest.mark.parametrize("path", ["/", "/logs", "/runs/1", "/?days=7"])
async def test_pages_redirect_to_login_when_unauthenticated(path, db):
    async with client() as c:
        r = await c.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


async def test_csv_refused_with_401(db):
    async with client() as c:
        assert (await c.get("/logs.csv")).status_code == 401


async def test_forged_or_expired_cookie_refused(db, monkeypatch):
    async with client() as c:
        c.cookies.set(auth.COOKIE, "eyJzdWIiOiJvd25lciJ9.forged.sig")
        assert (await c.get("/")).status_code == 303
    good = auth.make_session("password")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password("a new password!!", iterations=1000))
    from app.config import get_settings

    get_settings.cache_clear()
    assert auth.read_session(good) is None  # changing the password signs everyone out


async def test_password_login_and_logout(db):
    async with client() as c:
        await login(c)
        assert (await c.get("/")).status_code == 200
        csrf = c.cookies.get(auth.CSRF_COOKIE)
        out = await c.post("/logout", data={"csrf": csrf})
        assert out.status_code == 303
        c.cookies.delete(auth.COOKIE)
        assert (await c.get("/")).status_code == 303


async def test_wrong_password_and_throttle(db):
    async with client() as c:
        page = await c.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        for _ in range(5):
            r = await c.post("/login", data={"password": "nope", "csrf": csrf})
            assert r.status_code == 401 and auth.COOKIE not in r.cookies
        r = await c.post("/login", data={"password": PASSWORD, "csrf": csrf})
        assert r.status_code == 429  # even the right password waits after 5 failures


async def test_login_requires_csrf(db):
    async with client() as c:
        r = await c.post("/login", data={"password": PASSWORD, "csrf": "x"})
        assert r.status_code == 400


async def test_open_redirect_blocked(db):
    async with client() as c:
        page = await c.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = await c.post("/login", data={"password": PASSWORD, "csrf": csrf, "next": "//evil.example"})
        assert r.headers["location"] == "/"


async def test_telegram_link_works_once_and_preview_safe(db):
    token = auth.make_login_token(111111111)
    async with client() as c:
        get1 = await c.get(f"/login/telegram?token={token}")
        get2 = await c.get(f"/login/telegram?token={token}")  # a link preview fetching it twice
        assert get1.status_code == get2.status_code == 200 and auth.COOKIE not in c.cookies
        csrf = c.cookies.get(auth.CSRF_COOKIE)
        ok = await c.post("/login/telegram", data={"token": token, "csrf": csrf})
        again = await c.post("/login/telegram", data={"token": token, "csrf": csrf})
    assert ok.status_code == 303 and again.status_code == 401


async def test_telegram_link_for_another_user_or_tampered_rejected(db):
    assert not auth.check_login_token(auth.make_login_token(999), consume=False)
    assert not auth.check_login_token(auth.make_login_token(111111111) + "x", consume=False)


async def test_security_headers(db):
    async with client() as c:
        r = await c.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in r.headers["content-security-policy"] and "<script>" not in r.text


def test_password_hash_roundtrip_and_no_dollar_signs():
    h = auth.hash_password("s3cret-pass", iterations=1000)
    assert auth.verify_password("s3cret-pass", h) and not auth.verify_password("other", h)
    assert "$" not in h and not auth.verify_password("x", "garbage")


# --------------------------------------------------------- aggregations ---

async def fixture_data():
    """Two models, known numbers. Today = 2026-10-27 (Tehran)."""
    async with session_scope() as s:
        u = User(telegram_user_id=111111111, name="Owner")
        s.add(u)
        await s.flush()

        async def run(model, at, status, intent="schedule", action=True, error=None, steps=2, rating=None,
                      json_fail=0, exec_ok=None):
            r = AgentRun(user_id=u.id, model=model, status=status, intent=intent, steps=steps, error=error,
                         action_json={"type": intent} if action else None, created_at=at, latency_ms=2000)
            s.add(r)
            await s.flush()
            for i in range(json_fail):
                s.add(ToolCall(run_id=r.id, tool_name="submit_schedule_action", status="failed",
                               result_json={"kind": "schema"}, idempotency_key=f"j{r.id}{i}", created_at=at))
            s.add(ToolCall(run_id=r.id, tool_name="submit_schedule_action", status="succeeded",
                           result_json={}, idempotency_key=f"t{r.id}", created_at=at))
            if exec_ok is not None:
                s.add(ToolCall(run_id=r.id, tool_name="create_calendar_event", status="succeeded" if exec_ok else "failed",
                               result_json={}, idempotency_key=f"run-{r.id}-execute", created_at=at))
            if rating:
                s.add(RunFeedback(run_id=r.id, user_id=u.id, rating=rating))
            return r

        today, yesterday, old = NOW.replace(hour=10), NOW - dt.timedelta(days=1), NOW - dt.timedelta(days=40)
        a = await run("flash", today, "executed", exec_ok=True, rating=1)
        b = await run("flash", today, "rejected", error="edit_consumed", rating=-1, json_fail=1)
        c = await run("flash", yesterday, "failed", exec_ok=False)
        d = await run("pro", yesterday, "rejected", error="cancelled")
        e = await run("pro", yesterday, "needs_clarification", intent="unknown", action=False)
        await run("flash", old, "executed", exec_ok=True)  # outside 30 days
        for r_, at, p, c_, cost, lat in [(a, today, 1000, 100, "0.002", 800), (b, today, 2000, 200, "0.004", 1200),
                                         (c, yesterday, 500, 50, "0.001", 400), (d, yesterday, 3000, 300, "0.010", 3000),
                                         (e, yesterday, 1500, 0, "0.003", 2000)]:
            s.add(LlmCall(run_id=r_.id, model=r_.model, purpose="intent", prompt_tokens=p, completion_tokens=c_,
                          cost_usd=Decimal(cost), latency_ms=lat, status="ok", created_at=at))
        s.add(LlmCall(model="flash", purpose="tool_step", prompt_tokens=0, completion_tokens=0, latency_ms=9000,
                      status="error", created_at=today))
    return {"a": a.id, "b": b.id, "c": c.id, "d": d.id, "e": e.id}


async def test_kpis(db):
    await fixture_data()
    async with session_scope() as s:
        k = await queries.kpis(s, NOW, TEHRAN, 30)
    assert k["tokens_today"] == 3300 and k["tokens_7d"] == 8650 and k["tokens_window"] == 8650
    assert k["cost_today"] == pytest.approx(0.006) and k["cost_window"] == pytest.approx(0.020)
    assert k["latency_p50"] == pytest.approx(1200) and k["latency_p95"] == pytest.approx(2800)  # error call excluded
    assert k["llm_errors"] == 1 and k["runs"] == 5
    assert k["success_rate"] == pytest.approx(4 / 5)                      # only c failed
    assert k["confirmation_rate"] == pytest.approx(2 / 4)                 # a, c confirmed of a,b,c,d decided
    assert k["edit_rate"] == pytest.approx(1 / 4)


async def test_quality_by_model(db):
    await fixture_data()
    async with session_scope() as s:
        rows = {r["model"]: r for r in await queries.quality_by_model(s, NOW, TEHRAN, 30)}
    f, p = rows["flash"], rows["pro"]
    assert f["runs"] == 3 and f["success_rate"] == pytest.approx(2 / 3)
    assert f["json_valid_rate"] == pytest.approx(1 - 1 / 4)  # 1 failure in 4 final-answer calls
    assert f["edit_rate"] == pytest.approx(1 / 3) and f["thumbs_up"] == pytest.approx(0.5) and f["rated"] == 2
    assert f["avg_cost"] == pytest.approx((0.002 + 0.004 + 0.001) / 3)
    assert p["runs"] == 2 and p["success_rate"] == 1.0 and p["edit_rate"] == 0.0 and p["thumbs_up"] is None


async def test_daily_series_and_top_runs(db):
    ids = await fixture_data()
    async with session_scope() as s:
        usage = await queries.daily_usage(s, NOW, TEHRAN, 7)
        lat = await queries.daily_latency(s, NOW, TEHRAN, 7)
        top = await queries.top_runs(s, NOW, TEHRAN, 30, limit=2)
    by = {(r["day"].isoformat(), r["model"], r["purpose"]): r["tokens"] for r in usage}
    assert by[("2026-10-27", "flash", "intent")] == 3300 and by[("2026-10-26", "pro", "intent")] == 4800
    assert [r["day"].isoformat() for r in lat] == ["2026-10-26", "2026-10-27"]
    assert [r["run_id"] for r in top] == [ids["d"], ids["b"]]


async def test_day_boundaries_use_tehran_time(db):
    async with session_scope() as s:
        # 21:00 UTC on Oct 26 is 00:30 on Oct 27 in Tehran: counts as "today".
        s.add(LlmCall(model="m", purpose="intent", prompt_tokens=10, completion_tokens=0, latency_ms=1, status="ok",
                      created_at=dt.datetime(2026, 10, 26, 21, 0, tzinfo=dt.timezone.utc)))
        await s.flush()
        k = await queries.kpis(s, NOW, TEHRAN, 30)
    assert k["tokens_today"] == 10


async def test_logs_filters_and_csv(db):
    ids = await fixture_data()
    async with session_scope() as s:
        rows, total = await queries.logs(s, queries.LogFilters(model="pro"), TEHRAN)
        assert total == 2 and {r["run_id"] for r in rows} == {ids["d"], ids["e"]}
        rows, total = await queries.logs(s, queries.LogFilters(date_from=dt.date(2026, 10, 27)), TEHRAN)
        assert {r["run_id"] for r in rows} == {ids["a"], ids["b"]}
        rows, total = await queries.logs(s, queries.LogFilters(status="failed"), TEHRAN)
        assert [r["run_id"] for r in rows] == [ids["c"]]
    async with client() as c:
        await login(c)
        r = await c.get("/logs.csv?model=pro")
        page = await c.get("/logs?status=failed")
        bad = await c.get("/logs?date_from=yesterday")
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert r.headers["content-type"].startswith("text/csv") and lines[0].startswith("run_id,created_at,model")
    assert len(lines) == 3 and "transcript" not in lines[0] and "body" not in lines[0]
    assert page.status_code == 200 and f"/runs/{ids['c']}" in page.text and bad.status_code == 422


async def test_csv_neutralises_formula_injection(db):
    async with session_scope() as s:
        u = User(telegram_user_id=5)
        s.add(u)
        await s.flush()
        s.add(AgentRun(user_id=u.id, model="m", status="failed", error="=HYPERLINK(\"http://evil\")"))
    async with client() as c:
        await login(c)
        body = (await c.get("/logs.csv")).content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body


async def test_pages_render_with_data_and_detail_shows_email_body(db):
    ids = await fixture_data()
    async with session_scope() as s:
        from app.db.models import Email

        vm = VoiceMessage(user_id=1, source="text", transcript="به سارا ایمیل بزن")
        s.add(vm)
        await s.flush()
        await s.execute(text("UPDATE agent_runs SET voice_message_id = :v WHERE id = :r"), {"v": vm.id, "r": ids["a"]})
        s.add(Email(run_id=ids["a"], to_address="sara@example.com", subject="موضوع", body="متن کامل ایمیل", status="sent"))
    async with client() as c:
        await login(c)
        overview = await c.get("/?days=30")
        logs = await c.get("/logs")
        detail = await c.get(f"/runs/{ids['a']}")
        missing = await c.get("/runs/999999")
    assert overview.status_code == 200 and "Tokens today" in overview.text and 'id="chart-data"' in overview.text
    assert "متن کامل ایمیل" not in overview.text and "متن کامل ایمیل" not in logs.text  # only on the detail page
    assert detail.status_code == 200 and "متن کامل ایمیل" in detail.text and "به سارا ایمیل بزن" in detail.text
    assert missing.status_code == 404


async def test_dashboard_sessions_are_read_only(db):
    from sqlalchemy.exc import DBAPIError

    from app.dashboard.app import db as dash_db

    gen = dash_db()
    session = await gen.__anext__()
    with pytest.raises(DBAPIError, match="read-only"):
        await session.execute(text("INSERT INTO users (telegram_user_id) VALUES (42)"))
    await session.rollback()
    await gen.aclose()


async def test_readonly_role_cannot_write_even_without_readonly_default(db):
    import psycopg

    from app.config import get_settings
    from app.db.roles import ensure_readonly_role

    s = get_settings()
    url = s.database_url().replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(url) as conn:
            ensure_readonly_role(conn, "dashboard_ro_test", "ro-pass-123", s.postgres_db)
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("test database user cannot create roles")
    ro = f"postgresql://dashboard_ro_test:ro-pass-123@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"
    with psycopg.connect(ro, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")  # even if a session switches it off...
        assert conn.execute("SELECT count(*) FROM run_quality").fetchone() is not None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # ...there is no write privilege
            conn.execute("INSERT INTO users (telegram_user_id) VALUES (43)")
