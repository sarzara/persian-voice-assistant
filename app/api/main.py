"""FastAPI service. Batch 01 only exposes a health check; endpoints arrive in batch 03."""

from fastapi import FastAPI

app = FastAPI(title="Persian Voice Assistant API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
