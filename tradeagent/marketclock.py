"""NYSE trading calendar without pandas, so hooks stay fast.

Holiday table covers 2026-2028. Extend `HOLIDAYS` / `EARLY_CLOSES` or use the
`market.extra_holidays` lever when the horizon runs out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)

# Full-day NYSE closures.
HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
    # 2028
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
    date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
    date(2028, 12, 25),
}

# 13:00 ET closes.
EARLY_CLOSES: set[date] = {
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
    date(2028, 7, 3), date(2028, 11, 24),
}


@dataclass(frozen=True)
class SessionWindow:
    open: datetime
    close: datetime
    early_close: bool


def now_et() -> datetime:
    return datetime.now(tz=ET)


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def is_trading_day(d: date, extra_holidays: set[date] | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    if d in HOLIDAYS:
        return False
    if extra_holidays and d in extra_holidays:
        return False
    return True


def session_window(d: date, extended: bool = False) -> SessionWindow:
    early = d in EARLY_CLOSES
    if extended:
        o, c = EXTENDED_OPEN, (time(17, 0) if early else EXTENDED_CLOSE)
    else:
        o, c = REGULAR_OPEN, (EARLY_CLOSE if early else REGULAR_CLOSE)
    return SessionWindow(
        open=datetime.combine(d, o, tzinfo=ET),
        close=datetime.combine(d, c, tzinfo=ET),
        early_close=early,
    )


def is_open(
    at: datetime | None = None,
    allowed_hours: str = "regular",
    extra_holidays: set[date] | None = None,
) -> tuple[bool, str]:
    """Return (open?, human reason)."""
    if allowed_hours == "any":
        return True, "allowed_hours=any"
    now = to_et(at or now_et())
    d = now.date()
    if not is_trading_day(d, extra_holidays):
        return False, f"{d} is not an NYSE trading day"
    w = session_window(d, extended=(allowed_hours == "extended"))
    if now < w.open:
        return False, f"before {allowed_hours} open ({w.open.strftime('%H:%M')} ET)"
    if now >= w.close:
        return False, f"after {allowed_hours} close ({w.close.strftime('%H:%M')} ET)"
    return True, "market open"


def past_cutoff(cutoff_hhmm: str, at: datetime | None = None) -> bool:
    now = to_et(at or now_et())
    hh, mm = (int(x) for x in cutoff_hhmm.split(":"))
    return now.time() >= time(hh, mm)


def trading_date(at: datetime | None = None) -> date:
    """Calendar date in ET; used to bucket 'today' for daily limits."""
    return to_et(at or now_et()).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def next_open(at: datetime | None = None, extra_holidays: set[date] | None = None) -> datetime:
    now = to_et(at or now_et())
    d = now.date()
    for _ in range(0, 10):
        if is_trading_day(d, extra_holidays):
            w = session_window(d)
            if now < w.open:
                return w.open
        d = d + timedelta(days=1)
        now = datetime.combine(d, time(0, 0), tzinfo=ET)
    raise RuntimeError("no trading day found within 10 days")


def is_last_trading_day_of_week(d: date, extra_holidays: set[date] | None = None) -> bool:
    n = d + timedelta(days=1)
    while n.weekday() < 5:
        if is_trading_day(n, extra_holidays):
            return False
        n += timedelta(days=1)
    return is_trading_day(d, extra_holidays)


def is_last_trading_day_of_month(d: date, extra_holidays: set[date] | None = None) -> bool:
    n = d + timedelta(days=1)
    while n.month == d.month:
        if is_trading_day(n, extra_holidays):
            return False
        n += timedelta(days=1)
    return is_trading_day(d, extra_holidays)
