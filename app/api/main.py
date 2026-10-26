"""FastAPI service. Batch 01 only exposes a health check; endpoints arrive in batch 03."""

from fastapi import FastAPI, Response, status
from sqlalchemy import text

from app.db.session import get_engine

app = FastAPI(title="Persian Voice Assistant API", version="0.1.0")


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    """Liveness plus a database round-trip, so the container healthcheck means something."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - report, don't crash the probe
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
