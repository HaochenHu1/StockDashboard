"""Time helpers. All market logic uses America/New_York."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SESSION_ZH = {"PRE": "盘前", "RTH": "盘中", "AFTER": "盘后", "OVERNIGHT": "夜盘", "CLOSED": "休市"}


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def et_date_str(now: dt.datetime | None = None) -> str:
    return (now or now_et()).strftime("%Y-%m-%d")


def et_str(now: dt.datetime | None = None) -> str:
    """'YYYY-MM-DD HH:MM:SS' in ET, the same format OpenD uses for US time_key."""
    return (now or now_et()).strftime("%Y-%m-%d %H:%M:%S")


def parse_hhmm(text: str) -> dt.time:
    text = str(text).strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    return dt.time(h, m, s)


def in_window(t: dt.time, start: dt.time, end: dt.time) -> bool:
    """start <= t < end; windows that cross midnight (e.g. 20:00-04:00) are supported."""
    if start == end:
        return True
    if start < end:
        return start <= t < end
    return t >= start or t < end


def us_session(now: dt.datetime | None = None) -> str:
    """Session by clock (holidays are not known here; OpenD market state is shown separately).

    PRE 04:00-09:30, RTH 09:30-16:00, AFTER 16:00-20:00 (Mon-Fri),
    OVERNIGHT 20:00-04:00 (Sun 20:00 through Fri 04:00).
    """
    now = (now or now_et()).astimezone(ET)
    wd = now.weekday()  # Mon=0 .. Sun=6
    t = now.time()
    four, nine30, sixteen, twenty = dt.time(4), dt.time(9, 30), dt.time(16), dt.time(20)
    if wd == 5:
        return "CLOSED"
    if wd == 6:
        return "OVERNIGHT" if t >= twenty else "CLOSED"
    if t < four:
        return "OVERNIGHT"  # Mon 00:00-04:00 continues Sunday night; Tue-Fri continue the prior night
    if t < nine30:
        return "PRE"
    if t < sixteen:
        return "RTH"
    if t < twenty:
        return "AFTER"
    return "CLOSED" if wd == 4 else "OVERNIGHT"
