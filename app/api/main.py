"""FastAPI service: health checks and the admin API. Listens on 127.0.0.1:8000 only (see compose)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.api import health
from app.api.admin import router as admin_router
from app.api.schemas import Health
from app.config import get_settings
from app.logging_setup import configure_logging, new_request_id, request_id_var, set_run_id

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    debug = settings.debug
    configure_logging("api", settings.log_level, settings.log_format)
    app = FastAPI(
        title="Persian Voice Assistant API",
        version="1.0.0",
        # Interactive docs and the schema are an information leak in production.
        docs_url="/docs" if debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if debug else None,
    )

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming[:64] if incoming.replace("-", "").isalnum() else None
        request_id_var.set(rid or new_request_id("api"))
        set_run_id(None)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id_var.get() or ""
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full details only in the logs; the client gets a generic message.
        log.exception("Unhandled API error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error", "request_id": request_id_var.get()}, status_code=500)

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health", response_model=Health, tags=["health"])
    async def ready(response: Response) -> Health:
        checks = await health.readiness()
        ok = all(c.ok for c in checks.values())
        if not ok:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="ok" if ok else "degraded", checks=checks)

    app.include_router(admin_router)
    return app


app = create_app()
