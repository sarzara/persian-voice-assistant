"""Persian text and date helpers: normalisation, digits, Jalali calendar."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

import jdatetime

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # Monday=0
JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_CHARS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "‌": " ", "ـ": ""})
_DIACRITICS = re.compile(r"[ً-ٰٟ]")
_PERSIAN_LETTER = re.compile(r"[؀-ۿ]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize(text: str) -> str:
    """Canonical form for matching: Persian letters, ASCII digits, no ZWNJ/diacritics, casefolded."""
    text = unicodedata.normalize("NFC", text).translate(_CHARS).translate(_DIGITS)
    text = _DIACRITICS.sub("", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def to_ascii_digits(text: str) -> str:
    return text.translate(_DIGITS)


def fa_digits(value: object) -> str:
    return str(value).translate(_TO_FA_DIGITS)


def has_persian(text: str) -> bool:
    return bool(_PERSIAN_LETTER.search(text))


def emails_in(text: str) -> set[str]:
    return {m.lower() for m in EMAIL_RE.findall(to_ascii_digits(text))}


def jalali(day: dt.date) -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=day)


def jalali_iso(day: dt.date) -> str:
    j = jalali(day)
    return f"{j.year:04d}-{j.month:02d}-{j.day:02d}"


def jalali_text(day: dt.date) -> str:
    """e.g. 'سه‌شنبه ۵ آبان ۱۴۰۵'."""
    j = jalali(day)
    return f"{WEEKDAYS_FA[day.weekday()]} {fa_digits(j.day)} {JALALI_MONTHS[j.month - 1]} {fa_digits(j.year)}"


def jalali_to_gregorian(year: int, month: int, day: int) -> dt.date:
    return jdatetime.date(year, month, day).togregorian()
