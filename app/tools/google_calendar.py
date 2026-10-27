"""Google Calendar API v3 client (OAuth 2.0 refresh-token flow, scope calendar.events).

Idempotency: every event is inserted with a client-supplied event id derived
from the run's idempotency key. A repeated insert (double tap, crash and
retry) gets HTTP 409 from Google, and we return the existing event instead
of creating a second one. That makes inserts safe to retry as well.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
RETRYABLE = {429, 500, 502, 503, 504}


class CalendarError(Exception):
    pass


class CalendarAuthError(CalendarError):
    pass


class CalendarNotConfigured(CalendarError):
    pass


@dataclass(frozen=True)
class CreatedEvent:
    id: str
    html_link: str | None
    duplicate: bool  # True when the event already existed (idempotent replay)


def event_id_for(idempotency_key: str) -> str:
    # Google event ids: base32hex chars (a-v, 0-9), 5-1024 long. Hex is a subset.
    return "pva" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]


class GoogleCalendarClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        timeout_s: float = 20.0,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise CalendarNotConfigured("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN are required")
        self._client_id, self._client_secret, self._refresh_token = client_id, client_secret, refresh_token
        self.calendar_id = calendar_id
        self._max_attempts = max_attempts
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kw: Any) -> GoogleCalendarClient:
        s = settings or get_settings()
        return cls(
            client_id=s.google_client_id,
            client_secret=s.google_client_secret.get_secret_value(),
            refresh_token=s.google_refresh_token.get_secret_value(),
            calendar_id=s.google_calendar_id,
            **kw,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry - 60:
                return self._token
            r = await self._http.post(TOKEN_URL, data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            })
            if r.status_code != 200:
                err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error")
                raise CalendarAuthError(
                    f"Google token refresh failed ({r.status_code} {err}). "
                    "If the refresh token expired or was revoked, run scripts/google_auth.py again."
                )
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.monotonic() + int(body.get("expires_in", 3600))
            return self._token

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = f"{API_BASE}/calendars/{quote(self.calendar_id, safe='')}{path}"
        for attempt in range(1, self._max_attempts + 1):
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            try:
                r = await self._http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise CalendarError(f"Google Calendar unreachable: {exc.__class__.__name__}") from exc
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            if r.status_code == 401 and attempt < self._max_attempts:
                self._token = None  # expired early; refresh once and retry
                continue
            if r.status_code in RETRYABLE and attempt < self._max_attempts:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            return r
        raise AssertionError("unreachable")  # pragma: no cover

    async def list_events(self, day: dt.date, tz: dt.tzinfo) -> list[dict[str, Any]]:
        start = dt.datetime.combine(day, dt.time.min, tz)
        r = await self._request("GET", "/events", params={
            "timeMin": start.isoformat(),
            "timeMax": (start + dt.timedelta(days=1)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
            "timeZone": str(tz),
        })
        if r.status_code != 200:
            raise CalendarError(f"list events failed: HTTP {r.status_code} {r.text[:200]}")
        return [
            {
                "title": e.get("summary", ""),
                "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            }
            for e in r.json().get("items", [])
            if e.get("status") != "cancelled"
        ]

    async def insert_event(
        self,
        *,
        event_id: str,
        start: dt.datetime,
        end: dt.datetime,
        timezone: str,
        title: str,
        description: str | None,
        attendees: list[str],
    ) -> CreatedEvent:
        body = {
            "id": event_id,
            "summary": title,
            "description": description or "",
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
            "attendees": [{"email": a} for a in attendees],
        }
        r = await self._request("POST", "/events", params={"sendUpdates": "all"}, json=body)
        if r.status_code in (200, 201):
            data = r.json()
            return CreatedEvent(id=data["id"], html_link=data.get("htmlLink"), duplicate=False)
        if r.status_code == 409:
            existing = await self._request("GET", f"/events/{event_id}")
            if existing.status_code == 200:
                data = existing.json()
                log.info("Calendar insert replayed: event %s already exists", event_id)
                return CreatedEvent(id=data["id"], html_link=data.get("htmlLink"), duplicate=True)
        raise CalendarError(f"insert event failed: HTTP {r.status_code} {r.text[:300]}")
