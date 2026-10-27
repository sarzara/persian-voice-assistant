"""FastAPI service: health checks and the admin API. Listens on 127.0.0.1:8000 only (see compose)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.api import health
from app.api.admin import router as admin_router
from app.api.schemas import Health
from app.config import get_settings

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    debug = get_settings().debug
    app = FastAPI(
        title="Persian Voice Assistant API",
        version="1.0.0",
        # Interactive docs and the schema are an information leak in production.
        docs_url="/docs" if debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if debug else None,
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full details only in the logs; the client gets a generic message.
        log.exception("Unhandled API error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

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
