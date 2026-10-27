import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app import budget
from app.agent.usage import CallRecord, record_llm_call
from app.budget import BudgetExceeded, after_llm_call, ensure_within_budget, period_starts, thresholds_to_alert
from app.db.models import AgentRun, BudgetAlert, LlmCall, User
from app.db.session import session_scope

TEHRAN = ZoneInfo("Asia/Tehran")


@pytest.fixture
def budgets(monkeypatch):
    def set_(daily=1000, monthly=0, hard=False, thresholds="80,100"):
        monkeypatch.setenv("TOKEN_BUDGET_DAILY", str(daily))
        monkeypatch.setenv("TOKEN_BUDGET_MONTHLY", str(monthly))
        monkeypatch.setenv("BUDGET_HARD_STOP", "true" if hard else "false")
        monkeypatch.setenv("BUDGET_ALERT_THRESHOLDS", thresholds)
        monkeypatch.setenv("OWNER_TELEGRAM_ID", "111111111")
        from app.config import get_settings

        get_settings.cache_clear()
    return set_


class Notifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


async def add_tokens(n: int, *, run_id=None, when=None):
    async with session_scope() as s:
        s.add(LlmCall(run_id=run_id, model="deepseek-v4-flash", purpose="intent", prompt_tokens=n,
                      completion_tokens=0, latency_ms=10, status="ok",
                      **({"created_at": when} if when else {})))


# ------------------------------------------------------------- pure logic ---

def test_thresholds_to_alert():
    assert thresholds_to_alert(79.9, [80, 100], set()) == []
    assert thresholds_to_alert(80, [100, 80], set()) == [80]
    assert thresholds_to_alert(130, [80, 100], {80}) == [100]
    assert thresholds_to_alert(130, [80, 100], {80, 100}) == []


def test_periods_follow_tehran_calendar():
    # 22:00 UTC on Oct 31 is 01:30 on Nov 1 in Tehran: a new day and a new month.
    starts = period_starts(dt.datetime(2026, 10, 31, 22, 0, tzinfo=dt.timezone.utc), TEHRAN)
    assert starts["daily"][1] == dt.date(2026, 11, 1) and starts["monthly"][1] == dt.date(2026, 11, 1)
    assert starts["daily"][0].isoformat() == "2026-11-01T00:00:00+03:30"


# ---------------------------------------------------------- with database ---

async def test_usage_excludes_demo_data_and_yesterday(db, budgets):
    budgets()
    async with session_scope() as s:
        demo = User(telegram_user_id=budget.DEMO_TELEGRAM_ID, name="Demo")
        s.add(demo)
        await s.flush()
        run = AgentRun(user_id=demo.id)
        s.add(run)
        await s.flush()
        demo_run = run.id
    await add_tokens(500)
    await add_tokens(10_000, run_id=demo_run)
    await add_tokens(7_000, when=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    async with session_scope() as s:
        daily, monthly = await budget.status(s)
    assert daily.used == 500 and daily.limit == 1000 and daily.pct == 50.0


async def test_alerts_once_at_80_and_once_at_100(db, budgets):
    budgets(daily=1000)
    notify = Notifier()
    await add_tokens(850)
    assert await after_llm_call(notifier=notify) == [("daily", 80)]
    assert await after_llm_call(notifier=notify) == []                 # not repeated
    await add_tokens(200)
    assert await after_llm_call(notifier=notify) == [("daily", 100)]
    assert [c for c, _ in notify.sent] == [111111111, 111111111]
    assert "۸۰٪" in notify.sent[0][1] and "۸۵۰ از ۱٬۰۰۰ توکن" in notify.sent[0][1]
    assert "تمام شد" in notify.sent[1][1]
    async with session_scope() as s:
        rows = (await s.execute(select(BudgetAlert).order_by(BudgetAlert.threshold))).scalars().all()
    assert [(r.threshold, r.sent) for r in rows] == [(80, True), (100, True)]


async def test_jumping_past_both_thresholds_sends_both(db, budgets):
    budgets(daily=1000, monthly=100_000)
    notify = Notifier()
    await add_tokens(5_000)
    alerted = await after_llm_call(notifier=notify)
    assert alerted == [("daily", 80), ("daily", 100)]  # monthly at 5 %: nothing


async def test_concurrent_checks_alert_exactly_once(db, budgets):
    budgets(daily=1000)
    notify = Notifier()
    await add_tokens(900)
    results = await asyncio.gather(*[after_llm_call(notifier=notify) for _ in range(5)])
    assert sum(len(r) for r in results) == 1 and len(notify.sent) == 1
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(BudgetAlert)) == 1


async def test_no_budget_configured_means_no_alerts(db, budgets):
    budgets(daily=0, monthly=0)
    await add_tokens(10**9)
    assert await after_llm_call(notifier=Notifier()) == []


async def test_hard_stop(db, budgets):
    budgets(daily=1000, hard=False)
    await add_tokens(1500)
    await ensure_within_budget()  # off: nothing happens
    budgets(daily=1000, hard=True)
    with pytest.raises(BudgetExceeded) as err:
        await ensure_within_budget()
    assert err.value.period == "daily"


async def test_provider_logging_triggers_alert_end_to_end(db, budgets, monkeypatch):
    """A real llm_calls write crosses the budget and the Telegram alert goes out."""
    budgets(daily=1000)
    sent = []

    async def fake_send_text(chat_id, text):
        sent.append(text)
        return True

    from app.bot import notify

    monkeypatch.setattr(notify, "send_text", fake_send_text)
    await record_llm_call(CallRecord("deepseek-v4-flash", "intent", 900, 50, 0, 120, 1, "ok"))
    assert len(sent) == 1 and "۸۰٪" in sent[0]


async def test_bot_refuses_new_requests_in_persian_when_stopped(db, budgets):
    budgets(daily=1000, hard=True)
    await add_tokens(2000)
    from app.bot import handlers
    from app.bot import messages as msg

    update = SimpleNamespace(effective_chat=None, effective_message=SimpleNamespace(reply_text=AsyncMock()))
    user = SimpleNamespace(id=1, timezone="Asia/Tehran", name="x")
    await handlers.respond_to(update, None, user, 1, "فردا جلسه")
    update.effective_message.reply_text.assert_awaited_once_with(msg.BUDGET_STOP_DAILY)
