"""Operator commands.

    python -m app.cli send-test-email            # to OWNER_EMAIL
    python -m app.cli send-test-email --to you@example.com
    python -m app.cli eval-agent                 # 30 Persian transcripts against the real LLM (accuracy report)
    python -m app.cli eval-agent --quick         # the 10 core samples
    python -m app.cli check-outbound             # can this server reach Telegram, DeepSeek, Resend, Google?

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


async def eval_agent(as_json: bool, quick: bool = False, out: str | None = None) -> int:
    import json

    from app.agent.evaluation import render_report, run_eval_set, run_live_eval
    from app.db.session import get_engine

    try:
        if not quick:
            summary, results, meta = await run_eval_set()
            report = render_report(summary, results, meta)
            if out:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(report if out.endswith(".md") else json.dumps(
                        {"summary": summary, "results": results, "meta": meta}, ensure_ascii=False, indent=1))
            print(json.dumps({"summary": summary, "results": results, "meta": meta}, ensure_ascii=False, indent=1)
                  if as_json else report)
            return 0 if summary["intent"]["accuracy"] == 1.0 and summary["safety"]["accuracy"] in (None, 1.0) else 1
        results = await run_live_eval()
    except ValueError as exc:  # missing DEEPSEEK_API_KEY
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    finally:
        await get_engine().dispose()

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    else:
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {r['id']:<45} {r['kind']:<9} steps={r['steps']} "
                  f"tokens={r['prompt_tokens']}+{r['completion_tokens']} {r['latency_ms']}ms")
            if not r["passed"]:
                print(f"      problems: {r['problems']}\n      detail:   {r['detail']}")
        passed = sum(r["passed"] for r in results)
        tokens = sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in results)
        print(f"\n{passed}/{len(results)} passed, {tokens} tokens total")
    return 0 if all(r["passed"] for r in results) else 1


async def check_outbound() -> int:
    """Reachability of every external service, from inside the container (same proxy settings as the app)."""
    import os
    import time

    import httpx

    s = get_settings()
    token = s.telegram_bot_token.get_secret_value()
    targets = [
        ("Telegram Bot API", f"{s.telegram_api_base_url.rstrip('/')}/bot{token}/getMe" if token
         else s.telegram_api_base_url, {}),
        ("DeepSeek API", s.deepseek_base_url.rstrip("/") + "/models",
         {"Authorization": f"Bearer {s.deepseek_api_key.get_secret_value()}"}),
        ("Resend API", s.resend_api_url.rstrip("/") + "/domains",
         {"Authorization": f"Bearer {s.resend_api_key.get_secret_value()}"}),
        ("Google OAuth", "https://oauth2.googleapis.com/token", {}),
        ("Google Calendar API", "https://www.googleapis.com/calendar/v3/users/me/calendarList", {}),
    ]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    print(f"Proxy: {proxy.split('@')[-1] if proxy else 'none'}\n")
    failures = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for name, url, headers in targets:
            started = time.perf_counter()
            try:
                r = await client.get(url, headers={k: v for k, v in headers.items() if v != "Bearer "})
                ms = int((time.perf_counter() - started) * 1000)
                # Any HTTP answer (even 401/404/405) proves the network path works.
                verdict = "OK " if r.status_code < 500 else "ERR"
                note = {401: " (reachable; key missing/invalid)", 403: " (403: may be a geo/sanctions block)",
                        405: " (reachable)", 404: " (reachable)"}.get(r.status_code, "")
                print(f"{verdict} {name:<22} HTTP {r.status_code} in {ms} ms{note}")
                failures += r.status_code >= 500 or r.status_code == 403
            except httpx.HTTPError as exc:
                failures += 1
                print(f"BLOCKED {name:<18} {exc.__class__.__name__}")
    print("\nAll reachable." if not failures else f"\n{failures} problem(s): see docs/DEPLOY.md 'Outbound access'.")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p_test = sub.add_parser("send-test-email", help="send one test email to prove RESEND_API_KEY works")
    p_test.add_argument("--to", help="recipient (default: OWNER_EMAIL)")
    sub.add_parser("check-outbound", help="check that Telegram, DeepSeek, Resend and Google are reachable")
    p_eval = sub.add_parser("eval-agent", help="run the 10 Persian sample requests against the real LLM")
    p_eval.add_argument("--json", action="store_true", help="machine-readable output")
    p_eval.add_argument("--quick", action="store_true", help="only the 10 core samples")
    p_eval.add_argument("--out", help="also write the report to this file (.md or .json)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "send-test-email":
        return asyncio.run(send_test_email(args.to))
    if args.command == "check-outbound":
        return asyncio.run(check_outbound())
    if args.command == "eval-agent":
        return asyncio.run(eval_agent(args.json, args.quick, args.out))
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
