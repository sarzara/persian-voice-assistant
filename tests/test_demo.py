import datetime as dt
import random
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.budget import DEMO_TELEGRAM_ID
from app.dashboard import demo
from app.db.models import AgentRun, LlmCall, User
from app.db.session import session_scope

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))


def test_plan_is_deterministic_and_covers_30_days():
    a, b = demo.plan_runs(30, NOW, random.Random(42)), demo.plan_runs(30, NOW, random.Random(42))
    assert [r["at"] for r in a] == [r["at"] for r in b]
    days = {r["at"].date() for r in a}
    assert len(days) == 30 and max(days) == dt.date(2026, 10, 26)  # today is left for real data
    assert all(1 <= r["steps"] <= 6 for r in a)
    assert {r["model"] for r in a} == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert {r["outcome"] for r in a} == {"action", "question", "failed"}


async def test_seed_is_repeatable_and_purgeable(db):
    n1 = await demo.seed(30, NOW)
    n2 = await demo.seed(30, NOW)  # re-seeding replaces, never duplicates
    async with session_scope() as s:
        runs = await s.scalar(select(func.count()).select_from(AgentRun))
        calls = await s.scalar(select(func.count()).select_from(LlmCall))
        q = (await s.execute(text("SELECT count(*) FILTER (WHERE decision IS NOT NULL) AS proposals, "
                                  "count(*) FILTER (WHERE rating IS NOT NULL) AS rated FROM run_quality"))).one()
    assert n1 == n2 == runs and calls > runs and q.proposals > 0 and q.rated > 0
    await demo.purge()
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert await s.scalar(select(func.count()).select_from(LlmCall)) == 0  # cascaded
        assert (await s.execute(select(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))).first() is None
