"""Minimal async client for the Resend email API (https://resend.com/docs/api-reference).

Only what the assistant needs: send one email, get the provider message id back.

Retry policy: 429 and 5xx responses are retried with exponential backoff
*only when an idempotency key is supplied*. Without one, a retry after a
timeout could deliver the same email twice, so the error is raised instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ResendError(Exception):
    def __init__(self, message: str, status_code: int | None = None, name: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.name = name


@dataclass(frozen=True)
class SentEmail:
    id: str


class ResendClient:
    def __init__(
        self,
        api_key: str,
        email_from: str,
        *,
        base_url: str = RESEND_API_URL,
        timeout_s: float = 15.0,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("RESEND_API_KEY is not set")
        if not email_from:
            raise ValueError("EMAIL_FROM is not set")
        self._from = email_from
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "persian-voice-assistant/0.1",
            },
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs) -> ResendClient:
        s = settings or get_settings()
        return cls(s.resend_api_key.get_secret_value(), s.email_from, **kwargs)

    async def __aenter__(self) -> ResendClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_email(
        self,
        to: str | list[str],
        subject: str,
        *,
        text: str,
        html: str | None = None,
        idempotency_key: str | None = None,
        reply_to: str | None = None,
    ) -> SentEmail:
        recipients = [to] if isinstance(to, str) else list(to)
        if not recipients:
            raise ValueError("at least one recipient is required")

        payload: dict[str, object] = {"from": self._from, "to": recipients, "subject": subject, "text": text}
        if html is not None:
            payload["html"] = html
        if reply_to:
            payload["reply_to"] = reply_to
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}

        attempts = self._max_attempts if idempotency_key else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post("/emails", json=payload, headers=headers)
            except httpx.TransportError as exc:
                if attempt < attempts:
                    await self._backoff(attempt, f"transport error: {exc!r}")
                    continue
                raise ResendError(f"could not reach Resend: {exc!r}") from exc

            if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                await self._backoff(attempt, f"HTTP {response.status_code}")
                continue
            return self._parse(response)

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse(response: httpx.Response) -> SentEmail:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success and isinstance(body, dict) and body.get("id"):
            return SentEmail(id=str(body["id"]))
        message = body.get("message") if isinstance(body, dict) else None
        name = body.get("name") if isinstance(body, dict) else None
        raise ResendError(
            f"Resend returned HTTP {response.status_code}: {message or response.text[:200]}",
            status_code=response.status_code,
            name=name,
        )

    @staticmethod
    async def _backoff(attempt: int, reason: str) -> None:
        delay = 0.5 * 2 ** (attempt - 1)
        log.warning("Resend send failed (%s); retrying in %.1fs", reason, delay)
        await asyncio.sleep(delay)
