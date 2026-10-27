"""Seed 30 days of realistic demo activity so the dashboard has something to show.

    python -m app.dashboard.demo            # (re)create demo data
    python -m app.dashboard.demo --purge    # remove it

Everything belongs to a dedicated demo user (telegram_user_id = -1). Deleting that user
cascades to its runs, tool calls, LLM calls and feedback, and budgets never count it.
The generator is seeded, so the numbers are the same on every run.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
import random
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.agent.evaluation import SAMPLES
from app.agent.usage import cost_usd
from app.budget import DEMO_TELEGRAM_ID
from app.db.models import AgentRun, LlmCall, RunFeedback, ToolCall, User, VoiceMessage
from app.db.session import get_engine, session_scope

TEHRAN = ZoneInfo("Asia/Tehran")
MODELS = [("deepseek-v4-flash", 0.85), ("deepseek-v4-pro", 0.15)]
LATENCY = {"deepseek-v4-flash": (7.0, 0.45), "deepseek-v4-pro": (7.8, 0.40)}  # lognormal mu, sigma (ms)


def _pick(rng: random.Random, weighted):
    r, acc = rng.random(), 0.0
    for value, w in weighted:
        acc += w
        if r <= acc:
            return value
    return weighted[-1][0]


def plan_runs(days: int, now: dt.datetime, rng: random.Random) -> list[dict]:
    """Pure generator (tested): one dict per run with its LLM calls, tool calls and outcome."""
    runs = []
    today = now.astimezone(TEHRAN).date()
    for back in range(days, 0, -1):
        day = today - dt.timedelta(days=back)
        weekend = day.weekday() == 4  # Friday
        for _ in range(rng.randint(2, 6) if weekend else rng.randint(6, 18)):
            at = dt.datetime.combine(day, dt.time(rng.randint(8, 21), rng.randint(0, 59)), TEHRAN)
            model = _pick(rng, MODELS)
            outcome = _pick(rng, [("action", 0.74), ("question", 0.20), ("failed", 0.06)])
            # Transcript consistent with the outcome (questions come from the "question" samples).
            sample = rng.choice([x for x in SAMPLES if (x.expect["kind"] == "action") == (outcome == "action")])
            json_fail = rng.random() < (0.04 if model == "deepseek-v4-pro" else 0.08)
            steps = min(6, (2 if outcome != "failed" else rng.randint(3, 6)) + (1 if json_fail else 0)
                        + (1 if rng.random() < 0.25 else 0))
            calls, prompt = [], rng.randint(1700, 2300)
            for i in range(steps):
                mu, sigma = LATENCY[model]
                err = rng.random() < 0.02
                completion = rng.randint(30, 90) if i < steps - 1 else rng.randint(120, 320)
                cached = int(prompt * rng.uniform(0.3, 0.75)) if i else int(prompt * rng.uniform(0.0, 0.5))
                calls.append({
                    "model": model, "purpose": "intent" if i == 0 else "tool_step",
                    "prompt_tokens": 0 if err else prompt, "completion_tokens": 0 if err else completion,
                    "cached_tokens": 0 if err else cached, "latency_ms": int(math.exp(rng.gauss(mu, sigma))),
                    "attempts": rng.randint(2, 4) if err else (2 if rng.random() < 0.03 else 1),
                    "status": "error" if err else "ok", "error": "LLM HTTP 503" if err else None,
                    "created_at": at + dt.timedelta(seconds=2 * i),
                })
                prompt += rng.randint(250, 600)
            decision = None
            if outcome == "action":
                decision = _pick(rng, [("confirmed", 0.76), ("edited", 0.11), ("cancelled", 0.08), ("expired", 0.05)])
            exec_ok = decision == "confirmed" and rng.random() > 0.03
            rating = None
            if rng.random() < 0.35:
                good = decision == "confirmed" or (outcome == "question" and rng.random() < 0.6)
                rating = 1 if (good and rng.random() < 0.9) or rng.random() < 0.1 else -1
            runs.append({"at": at, "model": model, "sample": sample, "outcome": outcome, "decision": decision,
                         "exec_ok": exec_ok, "json_fail": json_fail, "steps": steps, "calls": calls, "rating": rating})
    return runs


async def purge() -> int:
    async with session_scope() as s:
        res = await s.execute(delete(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))
        return res.rowcount or 0


async def seed(days: int = 30, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(TEHRAN)
    await purge()
    runs = plan_runs(days, now, random.Random(42))
    async with session_scope() as s:
        user = User(telegram_user_id=DEMO_TELEGRAM_ID, name="Demo", email="demo@example.com", timezone="Asia/Tehran")
        s.add(user)
        await s.flush()
        for n, r in enumerate(runs):
            vm = VoiceMessage(user_id=user.id, source="voice", telegram_file_id=f"demo-{n}",
                              duration_s=random.Random(n).randint(3, 25), transcript=r["sample"].transcript,
                              stt_confidence=round(random.Random(-n).uniform(0.62, 0.97), 2),
                              created_at=r["at"] - dt.timedelta(seconds=5))
            s.add(vm)
            await s.flush()
            exp = r["sample"].expect
            intent = exp.get("type", "unknown") if r["outcome"] == "action" else "unknown"
            if r["outcome"] == "action" and intent == "unknown":
                intent = "schedule"
            status = {"question": "needs_clarification", "failed": "failed"}.get(r["outcome"])
            error = None
            if r["outcome"] == "failed":
                error = "no valid final answer within 6 steps"
            elif r["decision"] == "confirmed":
                status = "executed" if r["exec_ok"] else "failed"
                error = None if r["exec_ok"] else "CalendarError: insert event failed: HTTP 503"
            elif r["decision"] in ("edited", "cancelled", "expired"):
                status, error = "rejected", {"edited": "edit_consumed", "cancelled": "cancelled", "expired": "expired"}[r["decision"]]
            action = None
            if r["outcome"] == "action":
                action = ({"type": "schedule", "date": (r["at"].date() + dt.timedelta(days=1)).isoformat(), "time": "10:00:00",
                           "duration_min": 30, "title": "جلسهٔ نمونه", "description": "", "guests": [], "warnings": []}
                          if intent == "schedule" else
                          {"type": "email", "to": {"name": "سارا", "email": "sara.mohammadi@example.com"},
                           "subject": "پیام نمونه", "body": "سلام، این یک ایمیل نمونه است."})
            elif r["outcome"] == "question":
                action = {"type": "clarify", "question": "چه ساعتی باشد؟"}
            ok_calls = [c for c in r["calls"] if c["status"] == "ok"]
            run = AgentRun(
                voice_message_id=vm.id, user_id=user.id, prompt_version="v1", steps=r["steps"], intent=intent,
                action_json=action, status=status or "pending_confirmation", model=r["model"], error=error,
                prompt_tokens=sum(c["prompt_tokens"] for c in ok_calls),
                completion_tokens=sum(c["completion_tokens"] for c in ok_calls),
                latency_ms=sum(c["latency_ms"] for c in r["calls"]) + 150, created_at=r["at"],
            )
            s.add(run)
            await s.flush()
            for c in r["calls"]:
                s.add(LlmCall(run_id=run.id, cost_usd=cost_usd(c["model"], c["prompt_tokens"], c["completion_tokens"],
                                                               c["cached_tokens"]), **c))
            t0 = r["at"]
            tools = [("get_current_datetime", "succeeded", None), ("find_contact", "succeeded", None)]
            if r["json_fail"]:
                tools.append(("submit_email_action" if intent == "email" else "submit_schedule_action", "failed", "schema"))
            terminal = {"action": "submit_schedule_action" if intent == "schedule" else "submit_email_action",
                        "question": "ask_user"}.get(r["outcome"])
            if terminal:
                tools.append((terminal, "succeeded", None))
            for i, (name, st, kind) in enumerate(tools):
                s.add(ToolCall(run_id=run.id, tool_name=name, arguments_json={}, status=st,
                               result_json={"error": "to: Input should be a valid dictionary", "kind": kind} if kind else {"ok": True},
                               idempotency_key=f"demo-{run.id}-{i}", latency_ms=random.Random(i + n).randint(3, 40),
                               created_at=t0 + dt.timedelta(seconds=i)))
            if r["decision"] == "confirmed":
                s.add(ToolCall(run_id=run.id, tool_name="create_calendar_event" if intent == "schedule" else "send_email",
                               arguments_json={}, status="succeeded" if r["exec_ok"] else "failed",
                               result_json={}, idempotency_key=f"run-{run.id}-execute",
                               latency_ms=random.Random(n).randint(250, 1400), created_at=t0 + dt.timedelta(minutes=1)))
            if r["rating"] is not None:
                s.add(RunFeedback(run_id=run.id, user_id=user.id, rating=r["rating"]))
    return len(runs)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.dashboard.demo")
    parser.add_argument("--purge", action="store_true", help="delete all demo data")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--if-empty", action="store_true",
                        help="seed only when the database has no runs at all (used by compose on first start)")
    args = parser.parse_args()

    async def go():
        try:
            if args.if_empty:
                async with session_scope() as s:
                    if (await s.execute(select(AgentRun.id).limit(1))).first() is not None:
                        print("Runs exist: demo data not seeded.")
                        return
            if args.purge:
                print(f"Removed demo user ({await purge()} row).")
            else:
                print(f"Seeded {await seed(args.days)} demo runs over {args.days} days (user telegram_user_id={DEMO_TELEGRAM_ID}).")
        finally:
            await get_engine().dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
