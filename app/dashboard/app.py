"""Usage & quality dashboard (read-only). FastAPI + Jinja2 + vendored Chart.js; no frontend build.

Runs as its own service on 127.0.0.1:8080, published on DASHBOARD_HOST by Caddy.
Every page except /login* and /healthz requires the owner's session.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app import budget
from app.config import get_settings
from app.dashboard import auth, queries
from app.dashboard.fmt import FILTERS
from app.logging_setup import configure_logging, new_request_id

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters.update(FILTERS)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@lru_cache
def engine() -> AsyncEngine:
    """Read-only role when configured; every session is a read-only transaction regardless."""
    s = get_settings()
    from sqlalchemy.engine import URL

    user, password = s.dashboard_db_user, s.dashboard_db_password.get_secret_value()
    if not password:
        log.warning("DASHBOARD_DB_PASSWORD not set: using the main role with read-only transactions")
        user, password = s.postgres_user, s.postgres_password.get_secret_value()
    url = URL.create("postgresql+psycopg", username=user, password=password, host=s.postgres_host,
                     port=s.postgres_port, database=s.postgres_db)
    return create_async_engine(url, pool_pre_ping=True, pool_size=3,
                               connect_args={"options": "-c default_transaction_read_only=on"})


async def db() -> AsyncSession:
    async with async_sessionmaker(engine(), expire_on_commit=False)() as session:
        yield session


class NotAuthenticated(Exception):
    pass


def owner(request: Request) -> dict:
    session = auth.read_session(request.cookies.get(auth.COOKIE))
    if session is None:
        raise NotAuthenticated()
    return session


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def _csrf(request: Request) -> str:
    return request.cookies.get(auth.CSRF_COOKIE) or secrets.token_urlsafe(24)


def _check_csrf(request: Request, token: str) -> None:
    cookie = request.cookies.get(auth.CSRF_COOKIE)
    if not cookie or not token or not secrets.compare_digest(cookie, token):
        raise HTTPException(400, "invalid form token; reload the page")


def _page(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    csrf = _csrf(request)
    resp = templates.TemplateResponse(request, template, {"csrf": csrf, "settings": get_settings(), **ctx},
                                      status_code=status_code)
    resp.set_cookie(auth.CSRF_COOKIE, csrf, httponly=True, samesite="strict", secure=auth.cookie_secure())
    return resp


def _login_response(next_url: str, method: str) -> RedirectResponse:
    resp = RedirectResponse(_safe_next(next_url), status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(method), max_age=get_settings().dashboard_session_hours * 3600,
                    httponly=True, samesite="lax", secure=auth.cookie_secure())
    return resp


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging("dashboard", s.log_level, s.log_format)
    app = FastAPI(title="Usage dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        new_request_id("dash")
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"  # login tokens must never leak via Referer
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated):
        path = request.url.path
        if path.endswith(".csv") or path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # ------------------------------------------------------------- login ---

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/"):
        return _page(request, "login.html", next=_safe_next(next), error=None,
                     password_enabled=bool(s.dashboard_password_hash.get_secret_value()))

    @app.post("/login")
    async def login(request: Request, password: str = Form(...), csrf: str = Form(""), next: str = Form("/")):
        _check_csrf(request, csrf)
        ip = _client_ip(request)
        stored = s.dashboard_password_hash.get_secret_value()
        if auth.throttle.blocked(ip):
            log.warning("Dashboard login throttled for %s", ip)
            return _page(request, "login.html", 429, next=_safe_next(next), password_enabled=True,
                         error="تلاش‌های ناموفق زیاد بود. چند دقیقهٔ دیگر دوباره امتحان کنید.")
        if not stored or not auth.verify_password(password, stored):
            auth.throttle.fail(ip)
            log.warning("Dashboard login failed from %s", ip)
            return _page(request, "login.html", 401, next=_safe_next(next), password_enabled=bool(stored),
                         error="رمز عبور درست نیست.")
        log.info("Dashboard login (password) from %s", ip)
        return _login_response(next, "password")

    @app.get("/login/telegram", response_class=HTMLResponse)
    async def telegram_confirm(request: Request, token: str = ""):
        # Shows a button only: link previews / prefetchers that GET this URL cannot sign in.
        valid = bool(token) and auth.check_login_token(token, consume=False)
        return _page(request, "telegram_login.html", 200 if valid else 401, token=token, valid=valid)

    @app.post("/login/telegram")
    async def telegram_login(request: Request, token: str = Form(...), csrf: str = Form("")):
        _check_csrf(request, csrf)
        if not auth.check_login_token(token, consume=True):
            return _page(request, "telegram_login.html", 401, token="", valid=False)
        log.info("Dashboard login (telegram link) from %s", _client_ip(request))
        return _login_response("/", "telegram")

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("")):
        _check_csrf(request, csrf)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ------------------------------------------------------------- pages ---

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, days: int = Query(30, ge=1, le=365), _=Depends(owner),
                       session: AsyncSession = Depends(db)):
        now, zone = dt.datetime.now(dt.timezone.utc), tz()
        k = await queries.kpis(session, now, zone, days)
        usage = await queries.daily_usage(session, now, zone, days)
        chart = {
            "days": [(queries.day_start(now, zone, days - 1 - i)).date().isoformat() for i in range(days)],
            "usage": [{**r, "day": r["day"].isoformat()} for r in usage],
            "latency": [{**r, "day": r["day"].isoformat()} for r in await queries.daily_latency(session, now, zone, days)],
            "quality": [{**r, "day": r["day"].isoformat()} for r in await queries.daily_quality(session, now, zone, days)],
        }
        return _page(
            request, "overview.html", k=k, days=days, chart_json=json.dumps(chart, default=str),
            models=await queries.quality_by_model(session, now, zone, days),
            top=await queries.top_runs(session, now, zone, days),
            budgets=await budget.status(session, now), hard_stop=s.budget_hard_stop,
            thresholds=s.budget_alert_thresholds_list, demo=await queries.has_demo_data(session),
        )

    def _filters(date_from: str | None, date_to: str | None, model: str | None, status: str | None,
                 intent: str | None) -> queries.LogFilters:
        def d(v):
            try:
                return dt.date.fromisoformat(v) if v else None
            except ValueError as exc:
                raise HTTPException(422, f"invalid date: {v!r}") from exc
        return queries.LogFilters(d(date_from), d(date_to), model or None, status or None, intent or None)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, date_from: str | None = None, date_to: str | None = None,
                        model: str | None = None, status: str | None = None, intent: str | None = None,
                        page: int = Query(1, ge=1), _=Depends(owner), session: AsyncSession = Depends(db)):
        f = _filters(date_from, date_to, model, status, intent)
        rows, total = await queries.logs(session, f, tz(), limit=50, offset=(page - 1) * 50)
        query = "&".join(f"{k}={quote(str(v))}" for k, v in
                         (("date_from", date_from), ("date_to", date_to), ("model", model), ("status", status),
                          ("intent", intent)) if v)
        return _page(request, "logs.html", rows=rows, total=total, page=page, pages=max(1, -(-total // 50)),
                     f=f, query=query, options=await queries.filter_options(session))

    @app.get("/logs.csv")
    async def logs_csv(date_from: str | None = None, date_to: str | None = None, model: str | None = None,
                       status: str | None = None, intent: str | None = None, _=Depends(owner),
                       session: AsyncSession = Depends(db)):
        rows, _total = await queries.logs(session, _filters(date_from, date_to, model, status, intent), tz(), limit=None)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=queries.LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            # Neutralise spreadsheet formula injection in free-text cells.
            writer.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
        name = f"runs-{dt.date.today().isoformat()}.csv"
        return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: int, _=Depends(owner), session: AsyncSession = Depends(db)):
        detail = await queries.run_detail(session, run_id)
        if detail is None:
            return _page(request, "not_found.html", 404, run_id=run_id)
        pretty = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)  # noqa: E731
        return _page(request, "run.html", r=detail, pretty=pretty)

    return app


app = create_app()
