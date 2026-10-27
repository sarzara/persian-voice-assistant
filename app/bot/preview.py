"""Readable Persian previews and result messages (plain text, never parse_mode)."""

from __future__ import annotations

import datetime as dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.agent.actions import EmailAction, ScheduleAction
from app.tools.persian import fa_digits, jalali_text

CONFIRM, EDIT, CANCEL = "confirm", "edit", "cancel"


def keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"run:{run_id}:{CONFIRM}"),
        InlineKeyboardButton("✏️ ویرایش", callback_data=f"run:{run_id}:{EDIT}"),
        InlineKeyboardButton("❌ لغو", callback_data=f"run:{run_id}:{CANCEL}"),
    ]])


def parse_callback(data: str) -> tuple[int, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "run" or not parts[1].isdigit() or parts[2] not in (CONFIRM, EDIT, CANCEL):
        return None
    return int(parts[1]), parts[2]


def _person(name: str | None, email: str) -> str:
    return f"{name} ‹{email}›" if name else email


def render_preview(action: ScheduleAction | EmailAction, *, notes: list[str] | None = None) -> str:
    if isinstance(action, ScheduleAction):
        start = dt.datetime.combine(action.date, action.time)
        end = start + dt.timedelta(minutes=action.duration_min)
        lines = [
            "📅 پیش‌نمایش جلسه",
            "",
            f"• تاریخ: {jalali_text(action.date)} ({action.date.isoformat()})",
            f"• ساعت: {fa_digits(start.strftime('%H:%M'))} تا {fa_digits(end.strftime('%H:%M'))} "
            f"({fa_digits(action.duration_min)} دقیقه، به وقت تهران)",
            f"• عنوان: {action.title}",
        ]
        if action.description:
            lines.append(f"• توضیحات: {action.description}")
        if action.guests:
            lines.append("• مهمانان: " + "، ".join(_person(g.name, g.email) for g in action.guests))
            lines.append("  (دعوت‌نامه برای مهمانان ایمیل می‌شود)")
        lines += [f"⚠️ {w}" for w in action.warnings]
    else:
        lines = [
            "✉️ پیش‌نمایش ایمیل",
            "",
            f"• به: {_person(action.to.name, action.to.email)}",
            f"• موضوع: {action.subject}",
            "• متن:",
            action.body,  # never truncated: the owner must see everything that will be sent
        ]
    lines += [f"ℹ️ {n}" for n in (notes or [])]
    lines += ["", "انجام شود؟"]
    return "\n".join(lines)


def render_executed(action: ScheduleAction | EmailAction, data: dict) -> str:
    if isinstance(action, ScheduleAction):
        text = "✅ جلسه در تقویم ثبت شد."
        if action.guests:
            text += " دعوت‌نامه برای مهمانان فرستاده شد."
        if data.get("html_link"):
            text += f"\n{data['html_link']}"
        return text
    return f"✅ ایمیل برای {_person(action.to.name, action.to.email)} فرستاده شد."


def render_failure(error: str | None) -> str:
    e = (error or "").lower()
    if "not configured" in e:
        hint = "اتصال به سرویس (تقویم گوگل یا ایمیل) هنوز تنظیم نشده است."
    elif "auth" in e or "token" in e or "invalid_grant" in e:
        hint = "دسترسی به تقویم گوگل منقضی شده است؛ باید دوباره مجوز داده شود."
    elif "resend" in e:
        hint = "سرویس ایمیل درخواست را نپذیرفت."
    else:
        hint = "سرویس مقصد خطا داد."
    return f"❌ انجام نشد. {hint}\nلطفاً بعداً دوباره درخواست را بفرستید."


def outcome_message(outcome) -> tuple[str, InlineKeyboardMarkup | None]:
    """Text (and buttons) for an AgentOutcome, shared by the bot and out-of-band notifications."""
    from app.bot import messages as msg

    if outcome.kind == "question":
        return outcome.question, None
    if outcome.kind == "action":
        return render_preview(outcome.action, notes=outcome.notes), keyboard(outcome.run_id)
    return msg.AGENT_FAILED, None
