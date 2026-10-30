"""Readable previews and result messages in Persian or English (plain text, never parse_mode)."""

from __future__ import annotations

import datetime as dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.agent.actions import EmailAction, ScheduleAction
from app.i18n import detect_text_language
from app.tools.persian import fa_digits, jalali_text

CONFIRM, EDIT, CANCEL = "confirm", "edit", "cancel"

BUTTONS = {
    "fa": ("✅ تأیید", "✏️ ویرایش", "❌ لغو"),
    "en": ("✅ Confirm", "✏️ Edit", "❌ Cancel"),
}


def action_language(action: ScheduleAction | EmailAction) -> str:
    """'fa' or 'en': the language the action was written in (the agent writes it in the owner's language)."""
    if isinstance(action, ScheduleAction):
        return detect_text_language(f"{action.title} {action.description}")
    return detect_text_language(f"{action.subject} {action.body}")


def _feedback_enabled() -> bool:
    from app.config import get_settings

    return get_settings().feedback_buttons


def feedback_row(run_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("👍", callback_data=f"fb:{run_id}:up"),
            InlineKeyboardButton("👎", callback_data=f"fb:{run_id}:down")]


def feedback_keyboard(run_id: int) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup([feedback_row(run_id)]) if _feedback_enabled() else None


def keyboard(run_id: int, lang: str = "fa") -> InlineKeyboardMarkup:
    confirm, edit, cancel = BUTTONS.get(lang, BUTTONS["fa"])
    rows = [[
        InlineKeyboardButton(confirm, callback_data=f"run:{run_id}:{CONFIRM}"),
        InlineKeyboardButton(edit, callback_data=f"run:{run_id}:{EDIT}"),
        InlineKeyboardButton(cancel, callback_data=f"run:{run_id}:{CANCEL}"),
    ]]
    if _feedback_enabled():
        rows.append(feedback_row(run_id))
    return InlineKeyboardMarkup(rows)


def parse_feedback(data: str) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "fb" or not parts[1].isdigit() or parts[2] not in ("up", "down"):
        return None
    return int(parts[1]), 1 if parts[2] == "up" else -1


def parse_callback(data: str) -> tuple[int, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "run" or not parts[1].isdigit() or parts[2] not in (CONFIRM, EDIT, CANCEL):
        return None
    return int(parts[1]), parts[2]


def _person(name: str | None, email: str) -> str:
    return f"{name} ‹{email}›" if name else email


def _render_preview_en(action: ScheduleAction | EmailAction, notes: list[str] | None) -> str:
    if isinstance(action, ScheduleAction):
        start = dt.datetime.combine(action.date, action.time)
        end = start + dt.timedelta(minutes=action.duration_min)
        lines = [
            "📅 Meeting preview",
            "",
            f"• Date: {action.date.strftime('%A, %d %B %Y')} ({action.date.isoformat()})",
            f"• Time: {start.strftime('%H:%M')} to {end.strftime('%H:%M')} "
            f"({action.duration_min} minutes, Tehran time)",
            f"• Title: {action.title}",
        ]
        if action.description:
            lines.append(f"• Description: {action.description}")
        if action.guests:
            lines.append("• Guests: " + ", ".join(_person(g.name, g.email) for g in action.guests))
            lines.append("  (guests receive an email invitation)")
        lines += [f"⚠️ {w}" for w in action.warnings]
    else:
        lines = [
            "✉️ Email preview",
            "",
            f"• To: {_person(action.to.name, action.to.email)}",
            f"• Subject: {action.subject}",
            "• Message:",
            action.body,  # never truncated: the owner must see everything that will be sent
        ]
    lines += [f"ℹ️ {n}" for n in (notes or [])]
    lines += ["", "Go ahead?"]
    return "\n".join(lines)


def render_preview(action: ScheduleAction | EmailAction, *, notes: list[str] | None = None, lang: str = "fa") -> str:
    if lang == "en":
        return _render_preview_en(action, notes)
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


def render_executed(action: ScheduleAction | EmailAction, data: dict, lang: str | None = None) -> str:
    lang = lang or action_language(action)
    if lang == "en":
        if isinstance(action, ScheduleAction):
            text = "✅ The meeting is in your calendar."
            if action.guests:
                text += " Invitations were sent to the guests."
            if data.get("html_link"):
                text += f"\n{data['html_link']}"
            return text
        return f"✅ Email sent to {_person(action.to.name, action.to.email)}."
    if isinstance(action, ScheduleAction):
        text = "✅ جلسه در تقویم ثبت شد."
        if action.guests:
            text += " دعوت‌نامه برای مهمانان فرستاده شد."
        if data.get("html_link"):
            text += f"\n{data['html_link']}"
        return text
    return f"✅ ایمیل برای {_person(action.to.name, action.to.email)} فرستاده شد."


def render_failure(error: str | None, lang: str = "fa") -> str:
    e = (error or "").lower()
    if lang == "en":
        if "not configured" in e:
            hint = "The connection to the service (Google Calendar or email) is not set up yet."
        elif "auth" in e or "token" in e or "invalid_grant" in e:
            hint = "Access to Google Calendar has expired; it needs to be authorised again."
        elif "resend" in e:
            hint = "The email service did not accept the request."
        else:
            hint = "The service returned an error."
        return f"❌ Not done. {hint}\nPlease send the request again later."
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

    lang = getattr(outcome, "language", "fa")
    if outcome.kind == "question":
        return outcome.question, feedback_keyboard(outcome.run_id)
    if outcome.kind == "action":
        return render_preview(outcome.action, notes=outcome.notes, lang=lang), keyboard(outcome.run_id, lang)
    return msg.t("AGENT_FAILED", lang), feedback_keyboard(outcome.run_id)
