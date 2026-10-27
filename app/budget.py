"""Token budgets: daily and monthly limits, alerts at thresholds, optional hard stop.

- Usage = prompt + completion tokens in llm_calls, excluding demo data, counted in the
  owner's timezone (DEFAULT_TIMEZONE): "today" and "this calendar month".
- After every LLM call the bot/API checks the budget. Crossing a threshold (default 80
  and 100 %) inserts a budget_alerts row; the unique key (period, start, threshold) makes
  the Telegram alert go out exactly once, even with concurrent calls in two processes.
- With BUDGET_HARD_STOP=true, new requests are refused once a budget is at 100 %.
  Confirming an existing preview still works: it uses no tokens.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db.models import AgentRun, BudgetAlert, LlmCall, User
from app.db.session import session_scope
from app.tools.persian import fa_digits

log = logging.getLogger(__name__)

DEMO_TELEGRAM_ID = -1  # demo user created by app.dashboard.demo; never counted


class BudgetExceeded(Exception):
    def __init__(self, period: str):
        super().__init__(f"{period} token budget exhausted")
        self.period = period


@dataclass(frozen=True)
class BudgetPeriod:
    period: str          # "daily" | "monthly"
    start: dt.date
    used: int
    limit: int           # 0 = no budget

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.limit if self.limit else 0.0

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.used >= self.limit


def period_starts(now: dt.datetime, tz: ZoneInfo) -> dict[str, tuple[dt.datetime, dt.date]]:
    local = now.astimezone(tz)
    day = local.date()
    month = day.replace(day=1)
    return {
        "daily": (dt.datetime.combine(day, dt.time.min, tz), day),
        "monthly": (dt.datetime.combine(month, dt.time.min, tz), month),
    }


def thresholds_to_alert(pct: float, thresholds: list[int], already: set[int]) -> list[int]:
    return [t for t in sorted(thresholds) if pct >= t and t not in already]


async def usage_since(session, since: dt.datetime) -> int:
    demo_runs = select(AgentRun.id).join(User, AgentRun.user_id == User.id).where(User.telegram_user_id == DEMO_TELEGRAM_ID)
    stmt = select(func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0)).where(
        LlmCall.created_at >= since, or_(LlmCall.run_id.is_(None), LlmCall.run_id.not_in(demo_runs)))
    return int(await session.scalar(stmt))


async def status(session, now: dt.datetime | None = None) -> list[BudgetPeriod]:
    s = get_settings()
    tz = ZoneInfo(s.default_timezone)
    now = now or dt.datetime.now(dt.timezone.utc)
    limits = {"daily": s.token_budget_daily, "monthly": s.token_budget_monthly}
    out = []
    for period, (since, start) in period_starts(now, tz).items():
        out.append(BudgetPeriod(period, start, await usage_since(session, since), limits[period]))
    return out


def alert_text(p: BudgetPeriod, threshold: int, hard_stop: bool) -> str:
    label = "امروز" if p.period == "daily" else "این ماه"
    nums = f"{fa_digits(f'{p.used:,}'.replace(',', '٬'))} از {fa_digits(f'{p.limit:,}'.replace(',', '٬'))} توکن"
    if threshold >= 100:
        tail = " درخواست‌های جدید تا پایان دوره پذیرفته نمی‌شوند." if hard_stop else ""
        return f"⛔️ بودجهٔ توکن {label} تمام شد ({nums}).{tail}"
    return f"⚠️ هشدار بودجه: مصرف توکن {label} به {fa_digits(threshold)}٪ سقف رسید ({nums})."


async def after_llm_call(now: dt.datetime | None = None, *, notifier=None) -> list[tuple[str, int]]:
    """Check budgets and send any newly crossed alerts. Returns the (period, threshold) pairs alerted."""
    s = get_settings()
    if not (s.token_budget_daily or s.token_budget_monthly):
        return []
    thresholds = s.budget_alert_thresholds_list
    sent: list[tuple[str, int, int, str]] = []  # (period, threshold, alert id, message)
    async with session_scope() as session:
        periods = await status(session, now)
        for p in periods:
            if not p.limit:
                continue
            already = set((await session.execute(select(BudgetAlert.threshold).where(
                BudgetAlert.period == p.period, BudgetAlert.period_start == p.start))).scalars())
            for t in thresholds_to_alert(p.pct, thresholds, already):
                inserted = (await session.execute(
                    insert(BudgetAlert).values(period=p.period, period_start=p.start, threshold=t,
                                               used_tokens=p.used, limit_tokens=p.limit)
                    .on_conflict_do_nothing(constraint="uq_budget_alerts_period_start_threshold")
                    .returning(BudgetAlert.id)
                )).scalar_one_or_none()
                if inserted is not None:  # we won: this process sends the alert
                    sent.append((p.period, t, inserted, alert_text(p, t, s.budget_hard_stop)))
        await session.commit()

    if sent and s.owner_telegram_id is not None:
        if notifier is None:
            from app.bot.notify import send_text as notifier
        for period, t, alert_id, message in sent:
            ok = await notifier(s.owner_telegram_id, message)
            log.warning("Budget alert %s %d%% sent=%s", period, t, ok, extra={"budget_period": period, "threshold": t})
            if ok:
                async with session_scope() as session:
                    (await session.get(BudgetAlert, alert_id)).sent = True
    return [(p, t) for p, t, _, _ in sent]


async def ensure_within_budget(now: dt.datetime | None = None) -> None:
    """Raise BudgetExceeded when the hard stop is on and a budget is exhausted."""
    s = get_settings()
    if not s.budget_hard_stop or not (s.token_budget_daily or s.token_budget_monthly):
        return
    async with session_scope() as session:
        for p in await status(session, now):
            if p.exhausted:
                raise BudgetExceeded(p.period)
