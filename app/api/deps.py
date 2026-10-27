"""Shared FastAPI dependencies: database session and admin authentication."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_sessionmaker


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = get_settings().admin_api_key.get_secret_value()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin API disabled: ADMIN_API_KEY is not set")
    # Constant-time comparison: no timing side channel on the key.
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-Admin-Key",
                            headers={"WWW-Authenticate": "X-Admin-Key"})


class Paging:
    def __init__(self, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        self.limit = limit
        self.offset = offset


Admin = Depends(require_admin)
