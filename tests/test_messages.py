"""Every bot text the code refers to exists, exactly once (guards against edit accidents)."""

import ast
import collections
import pathlib
import re

from app.bot import messages as msg

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_referenced_message_exists():
    used = set()
    for path in (ROOT / "app").rglob("*.py"):
        used |= set(re.findall(r"\bmsg\.([A-Z][A-Z0-9_]+)\b", path.read_text(encoding="utf-8")))
    missing = sorted(name for name in used if not hasattr(msg, name))
    assert not missing, f"used but not defined in app/bot/messages.py: {missing}"


def test_no_message_defined_twice():
    tree = ast.parse((ROOT / "app" / "bot" / "messages.py").read_text(encoding="utf-8"))
    names = [t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)]
    dupes = [k for k, c in collections.Counter(names).items() if c > 1]
    assert not dupes, f"defined twice: {dupes}"


async def test_confirming_a_cancelled_request_answers_in_persian(monkeypatch):
    """The exact path that used to crash: tap Confirm on a run that was already cancelled."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.agent.executor import ExecResult, ExecStatus
    from app.bot import handlers

    async def fake_exec(*a, **k):
        return ExecResult(ExecStatus.CANCELLED)

    monkeypatch.setattr(handlers.executor, "confirm_and_execute", fake_exec)
    monkeypatch.setattr(handlers.pipeline, "get_calendar", lambda: None)
    monkeypatch.setattr(handlers.pipeline, "get_mailer", lambda: None)
    reply = AsyncMock()
    query = SimpleNamespace(data="run:9:confirm", answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=SimpleNamespace(reply_text=reply))
    await handlers.on_callback(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1)), None)
    assert reply.await_args.args[0] == msg.ALREADY_CANCELLED

def test_every_text_has_both_languages_with_the_same_placeholders():
    for key, versions in msg.TEXTS.items():
        assert set(versions) == {"fa", "en"}, key
        assert sorted(re.findall(r"{\w+}", versions["fa"])) == sorted(re.findall(r"{\w+}", versions["en"])), key


def test_t_picks_language_and_digits():
    assert msg.t("VOICE_TOO_LONG", "en", limit=60).startswith("That voice message is too long (more than 60")
    assert "۶۰" in msg.t("VOICE_TOO_LONG", "fa", limit=60)
    assert msg.t("CANCELLED", "xx") == msg.t("CANCELLED", "fa")   # unknown language falls back to Persian
    assert msg.START == msg.TEXTS["START"]["fa"]                   # old names still work
    

