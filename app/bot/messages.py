"""All user-facing bot text, in Persian and English, in one place.

Use t("KEY", lang, **values). The old module constants (msg.START, ...) still exist and
return the Persian text, so code that has not been converted yet keeps working.
"""

from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "REFUSAL": {
        "fa": "متأسفم، این دستیار خصوصی است و شما اجازهٔ استفاده از آن را ندارید. 🙏",
        "en": "Sorry, this is a private assistant and you are not allowed to use it. 🙏",
    },
    "START": {
        "fa": (
            "سلام{name}! 👋\n"
            "من دستیار صوتی شخصی شما هستم.\n\n"
            "می‌توانید با یک پیام صوتی از من بخواهید جلسه‌ای در تقویم ثبت کنم "
            "یا ایمیلی بفرستم؛ هیچ کاری بدون تأیید شما انجام نمی‌شود.\n\n"
            "پیام صوتی یا متنی بفرستید؛ پیش‌نمایش کار را می‌بینید و فقط با تأیید شما انجام می‌شود.\n"
            "برای راهنما /help و برای تغییر زبان /language را بفرستید."
        ),
        "en": (
            "Hi{name}! 👋\n"
            "I'm your personal voice assistant.\n\n"
            "Send me a voice or text message to schedule a meeting in your calendar or to send an "
            "email. You always see a preview first, and nothing happens without your confirmation.\n\n"
            "Send /help for help and /language to change the language."
        ),
    },
    "HELP": {
        "fa": (
            "راهنما:\n"
            "• /start — شروع و معرفی\n"
            "• /help — همین راهنما\n"
            "• /language — زبان: فارسی، انگلیسی یا خودکار\n"
            "• پیام صوتی یا متنی — مثلاً «فردا ساعت ده صبح با علی جلسه بگذار»\n"
            "  یا «به سارا ایمیل بزن و بگو گزارش را فردا می‌فرستم»\n"
            "• قبل از هر کاری پیش‌نمایش می‌بینید: تأیید، ویرایش یا لغو\n"
            "• هیچ ایمیلی فرستاده و هیچ جلسه‌ای ثبت نمی‌شود مگر با تأیید شما"
        ),
        "en": (
            "Help:\n"
            "• /start — introduction\n"
            "• /help — this help\n"
            "• /language — language: Persian, English or automatic\n"
            "• Voice or text, e.g. \"Schedule a meeting with Ali tomorrow at 10 am\"\n"
            "  or \"Email Sara that I'll send the report tomorrow\"\n"
            "• You always see a preview first: Confirm, Edit or Cancel\n"
            "• No email is sent and no meeting is created without your confirmation"
        ),
    },
    "STT_EMPTY": {
        "fa": "متأسفم، چیزی از پیام صوتی‌تان متوجه نشدم. لطفاً در جای کم‌سروصدا و کمی شمرده‌تر دوباره بفرستید.",
        "en": "Sorry, I couldn't make out your voice message. Please send it again from a quieter place and a bit more slowly.",
    },
    "STT_LOW_CONFIDENCE": {
        "fa": "مطمئن نیستم درست شنیده باشم. برداشت من این بود:\n«{text}»\nلطفاً پیام را واضح‌تر تکرار کنید یا آن را بنویسید.",
        "en": "I'm not sure I heard that correctly. I understood:\n\"{text}\"\nPlease repeat it more clearly or type it.",
    },
    "STT_FAILED": {
        "fa": "متأسفم، در تبدیل پیام صوتی به متن مشکلی پیش آمد. لطفاً دوباره بفرستید یا درخواست را بنویسید.",
        "en": "Sorry, something went wrong while transcribing your voice message. Please send it again or type your request.",
    },
    "VOICE_TOO_LONG": {
        "fa": "پیام صوتی خیلی طولانی است (بیشتر از {limit} ثانیه). لطفاً کوتاه‌تر بفرستید.",
        "en": "That voice message is too long (more than {limit} seconds). Please send a shorter one.",
    },
    "VOICE_TOO_LARGE": {
        "fa": "این پیام صوتی بزرگ‌تر از حد مجاز تلگرام (۲۰ مگابایت) است. لطفاً پیام کوتاه‌تری بفرستید.",
        "en": "This voice message is larger than Telegram's limit for bots (20 MB). Please send a shorter one.",
    },
    "TRANSCRIPT": {
        "fa": "🎙️ متن پیام شما:\n«{text}»",
        "en": "🎙️ Your message:\n\"{text}\"",
    },
    "AGENT_FAILED": {
        "fa": (
            "متأسفم، نتوانستم درخواست شما را به یک کار مشخص تبدیل کنم. "
            "لطفاً آن را ساده‌تر بگویید؛ مثلاً «فردا ساعت ده با علی جلسه بگذار» یا «به سارا ایمیل بزن که …»."
        ),
        "en": (
            "Sorry, I couldn't turn your request into a specific action. Please say it more simply, "
            "e.g. \"Schedule a meeting with Ali tomorrow at 10\" or \"Email Sara that …\"."
        ),
    },
    "AGENT_UNAVAILABLE": {
        "fa": "سرویس هوش مصنوعی فعلاً در دسترس نیست. لطفاً کمی بعد دوباره امتحان کنید.",
        "en": "The AI service is not available right now. Please try again in a little while.",
    },
    "ALREADY_DONE": {
        "fa": "این درخواست قبلاً انجام شده است؛ دوباره انجامش نمی‌دهم.",
        "en": "This request has already been carried out; I won't do it twice.",
    },
    "ALREADY_CANCELLED": {
        "fa": "این درخواست قبلاً لغو یا ویرایش شده است.",
        "en": "This request was already cancelled or edited.",
    },
    "EXPIRED": {
        "fa": "این پیش‌نمایش منقضی شده است. لطفاً درخواست را دوباره بفرستید.",
        "en": "This preview has expired. Please send your request again.",
    },
    "RUN_NOT_FOUND": {
        "fa": "این درخواست پیدا نشد.",
        "en": "This request could not be found.",
    },
    "CANCELLED": {
        "fa": "لغو شد. کاری انجام نشد.",
        "en": "Cancelled. Nothing was done.",
    },
    "BUDGET_STOP_DAILY": {
        "fa": "سقف مصرف امروز تمام شده است؛ درخواست‌های جدید از فردا پذیرفته می‌شوند. پیش‌نمایش‌های قبلی را هنوز می‌توانید تأیید کنید.",
        "en": "Today's usage limit has been reached; new requests are accepted again tomorrow. You can still confirm earlier previews.",
    },
    "BUDGET_STOP_MONTHLY": {
        "fa": "سقف مصرف این ماه تمام شده است؛ درخواست‌های جدید از ماه بعد پذیرفته می‌شوند. پیش‌نمایش‌های قبلی را هنوز می‌توانید تأیید کنید.",
        "en": "This month's usage limit has been reached; new requests are accepted again next month. You can still confirm earlier previews.",
    },
    "FEEDBACK_THANKS": {
        "fa": "ممنون از بازخوردتان 🙏",
        "en": "Thanks for your feedback 🙏",
    },
    "EDIT_PROMPT": {
        "fa": "چه چیزی را تغییر دهم؟ تغییر را بگویید یا بنویسید؛ مثلاً «ساعت را یازده کن» یا «سارا را هم دعوت کن».",
        "en": "What should I change? Say or type the change, e.g. \"make it 11\" or \"invite Sara too\".",
    },
    "UNSUPPORTED": {
        "fa": "فعلاً فقط پیام صوتی و پیام متنی پشتیبانی می‌شود.",
        "en": "For now only voice and text messages are supported.",
    },
    "GENERIC_ERROR": {
        "fa": "متأسفم، مشکلی پیش آمد و درخواست شما انجام نشد. لطفاً کمی بعد دوباره امتحان کنید.\nکد پیگیری: {ref}",
        "en": "Sorry, something went wrong and your request was not carried out. Please try again later.\nReference: {ref}",
    },
    "RATE_LIMITED": {
        "fa": "پیام‌ها خیلی پشت سر هم است. لطفاً حدود {minutes} دقیقهٔ دیگر دوباره بفرستید.",
        "en": "Too many messages in a row. Please try again in about {minutes} minutes.",
    },
    "DASHBOARD_LINK": {
        "fa": "پیوند ورود به داشبورد (یک‌بارمصرف، تا ۱۰ دقیقه):\n{link}",
        "en": "Dashboard sign-in link (single use, valid for 10 minutes):\n{link}",
    },
    "DASHBOARD_OWNER_ONLY": {
        "fa": "داشبورد فقط برای مالک دستیار در دسترس است.",
        "en": "The dashboard is only available to the assistant's owner.",
    },
    "DASHBOARD_NOT_CONFIGURED": {
        "fa": "داشبورد هنوز تنظیم نشده است (DASHBOARD_SECRET_KEY).",
        "en": "The dashboard is not configured yet (DASHBOARD_SECRET_KEY).",
    },
    "WORKING": {
        "fa": "در حال انجام…",
        "en": "Working…",
    },
    "LANGUAGE_USAGE": {
        "fa": (
            "زبان فعلی: {current}\n\n"
            "برای تغییر بفرستید:\n"
            "/language fa — فارسی\n"
            "/language en — English\n"
            "/language auto — خودکار (پاسخ به زبان پیام شما)"
        ),
        "en": (
            "Current language: {current}\n\n"
            "To change it, send:\n"
            "/language fa — Persian\n"
            "/language en — English\n"
            "/language auto — automatic (I answer in the language of your message)"
        ),
    },
    "LANGUAGE_SET": {
        "fa": "✅ زبان روی «{name}» تنظیم شد.",
        "en": "✅ Language set to {name}.",
    },
}

LANGUAGES = ("fa", "en")

# Names of the /language settings, in each display language.
LANGUAGE_NAMES = {
    "fa": {"fa": "فارسی", "en": "انگلیسی", "auto": "خودکار"},
    "en": {"fa": "Persian", "en": "English", "auto": "automatic"},
}

COMMANDS = [
    ("start", "شروع و معرفی"),
    ("help", "راهنما"),
    ("language", "زبان: فارسی / English / خودکار"),
    ("dashboard", "پیوند ورود به داشبورد مصرف"),
]


def to_persian_digits(value: object) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def t(key: str, lang: str = "fa", **values: object) -> str:
    """Text KEY in LANG ('fa' or 'en'). Numbers are written with Persian digits in Persian."""
    lang = lang if lang in LANGUAGES else "fa"
    if lang == "fa":
        values = {k: to_persian_digits(v) if isinstance(v, int) else v for k, v in values.items()}
    return TEXTS[key][lang].format(**values)


# Backwards compatibility: msg.START, msg.HELP, ... are the Persian texts, as before.
for _key, _value in TEXTS.items():
    globals()[_key] = _value["fa"]