"""Persian-locale number and date formatting for templates."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.tools.persian import fa_digits, jalali

TEHRAN = ZoneInfo("Asia/Tehran")


def fa_num(value, decimals: int = 0) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.{decimals}f}" if decimals else f"{round(float(value)):,}"
    # Persian thousands separator (٬) and decimal separator (٫).
    return fa_digits(text.replace(",", "٬").replace(".", "٫"))


def fa_compact(value) -> str:
    if value is None:
        return "—"
    v = float(value)
    for limit, suffix in ((1e9, " میلیارد"), (1e6, " میلیون"), (1e3, " هزار")):
        if abs(v) >= limit:
            return fa_num(v / limit, 1).rstrip("۰").rstrip("٫") + suffix
    return fa_num(v)


def fa_pct(value, decimals: int = 0) -> str:
    return "—" if value is None else fa_num(100 * float(value), decimals) + "٪"


def fa_usd(value) -> str:
    if value is None:
        return "—"
    v = float(value)
    return "$" + fa_num(v, 4 if 0 < abs(v) < 0.01 else 2)


def fa_ms(value) -> str:
    if value is None:
        return "—"
    v = float(value)
    return fa_num(v / 1000, 1) + " ث" if v >= 1000 else fa_num(v) + " ms"


def fa_dt(value: dt.datetime | None, tz: ZoneInfo = TEHRAN) -> str:
    if value is None:
        return "—"
    local = value.astimezone(tz)
    j = jalali(local.date())
    return fa_digits(f"{j.year}/{j.month:02d}/{j.day:02d} {local:%H:%M}")


def fa_date(value: dt.date | None) -> str:
    if value is None:
        return "—"
    j = jalali(value)
    return fa_digits(f"{j.month:02d}/{j.day:02d}")


FILTERS = {"fa_num": fa_num, "fa_compact": fa_compact, "fa_pct": fa_pct, "fa_usd": fa_usd, "fa_ms": fa_ms,
           "fa_dt": fa_dt, "fa_date": fa_date, "fa_digits": fa_digits}
