"""Operator commands.

    python -m app.cli send-test-email            # to OWNER_EMAIL
    python -m app.cli send-test-email --to you@example.com

In Docker:  docker compose run --rm api python -m app.cli send-test-email
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import sys
import uuid
from datetime import datetime, timezone

from app.config import get_settings
from app.tools.resend_client import ResendClient, ResendError

log = logging.getLogger("app.cli")

TEST_SUBJECT = "ایمیل آزمایشی دستیار صوتی"
TEST_TEXT = (
    "سلام!\n\n"
    "این یک ایمیل آزمایشی از دستیار صوتی شخصی شماست. "
    "اگر آن را دریافت کرده‌اید، کلید Resend و نشانی فرستنده درست تنظیم شده‌اند.\n\n"
    "زمان ارسال (UTC): {sent_at}"
)


async def _record(to: str, subject: str, body: str, provider_id: str | None, status: str) -> None:
    """Best effort: a database outage must not hide whether the email was sent."""
    try:
        from app.db.models import Email
        from app.db.repo import audit
        from app.db.session import get_engine, session_scope

        async with session_scope() as session:
            session.add(Email(to_address=to, subject=subject, body=body, provider_message_id=provider_id, status=status))
            await audit(session, "email.test_sent" if status == "sent" else "email.test_failed", "cli", to=to)
        await get_engine().dispose()
    except Exception as exc:  # noqa: BLE001
        log.warning("Email result not recorded in the database: %s", exc)


async def send_test_email(to: str | None) -> int:
    settings = get_settings()
    recipient = to or settings.owner_email
    if not recipient:
        print("No recipient: set OWNER_EMAIL in .env or pass --to.", file=sys.stderr)
        return 2

    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = TEST_TEXT.format(sent_at=sent_at)
    body_html = '<div dir="rtl" style="font-family:Tahoma,sans-serif;line-height:1.8">{}</div>'.format(
        html.escape(text).replace("\n", "<br>")
    )

    try:
        async with ResendClient.from_settings() as client:
            sent = await client.send_email(
                recipient, TEST_SUBJECT, text=text, html=body_html, idempotency_key=f"test-email-{uuid.uuid4()}"
            )
    except (ResendError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        await _record(recipient, TEST_SUBJECT, text, None, "failed")
        return 1

    await _record(recipient, TEST_SUBJECT, text, sent.id, "sent")
    print(f"OK: test email accepted by Resend for {recipient} (id={sent.id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p_test = sub.add_parser("send-test-email", help="send one test email to prove RESEND_API_KEY works")
    p_test.add_argument("--to", help="recipient (default: OWNER_EMAIL)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "send-test-email":
        return asyncio.run(send_test_email(args.to))
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
