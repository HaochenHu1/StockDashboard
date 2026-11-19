"""Pure calculations for the market overview page (unit tested)."""
from __future__ import annotations

import math
import re
from typing import Any, Iterable


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pct_change(last: Any, prev: Any) -> float | None:
    last, prev = _num(last), _num(prev)
    if last is None or prev is None or prev <= 0:
        return None
    return (last / prev - 1) * 100


def weighted_change(constituents: Iterable[dict]) -> float | None:
    """Market-cap weighted average of constituents' percent change.

    Constituents without a percent change or with a missing / non-positive market cap
    are left out. Returns None when nothing usable remains.
    """
    total_w = total = 0.0
    for c in constituents:
        chg, cap = _num(c.get("change_pct")), _num(c.get("market_cap"))
        if chg is None or cap is None or cap <= 0:
            continue
        total += chg * cap
        total_w += cap
    return total / total_w if total_w > 0 else None


def passes_size_filter(c: dict, min_market_cap: float, min_dollar_volume: float) -> bool:
    cap, dv = _num(c.get("market_cap")), _num(c.get("dollar_volume"))
    return cap is not None and dv is not None and cap >= min_market_cap and dv >= min_dollar_volume


def sector_leader(constituents: Iterable[dict], min_market_cap: float, min_dollar_volume: float) -> dict | None:
    """Top gainer among constituents that pass the size filter (ties: larger dollar volume)."""
    best = None
    for c in constituents:
        if _num(c.get("change_pct")) is None or not passes_size_filter(c, min_market_cap, min_dollar_volume):
            continue
        key = (c["change_pct"], _num(c.get("dollar_volume")) or 0.0)
        if best is None or key > best[0]:
            best = (key, c)
    return best[1] if best else None


def up_down_counts(constituents: Iterable[dict]) -> tuple[int, int, int]:
    up = down = flat = 0
    for c in constituents:
        chg = _num(c.get("change_pct"))
        if chg is None:
            continue
        if chg > 0:
            up += 1
        elif chg < 0:
            down += 1
        else:
            flat += 1
    return up, down, flat


# Plates whose members ARE China-concept companies (热门中概股, China's Terrific): the source of the exclusion list.
CHINA_STOCKS_PLATE_RE = re.compile(r"中概|中国|内地|大陆|中资|China|Chinese|大中华|Greater\s*China", re.IGNORECASE)
# Plates that are China themes and are hidden, but whose members are not all Chinese
# (腾讯概念 also holds Spotify, so its members must not be excluded everywhere).
CHINA_THEME_RE = re.compile(
    r"港股|A股|香港|台湾|人民币|双十一|双11|春节"
    r"|腾讯|阿里|百度|小米|华为|字节|抖音|DeepSeek|拼多多|京东|美团|蔚来|小鹏|理想汽车|比亚迪|宁德|中芯|网易|哔哩|携程",
    re.IGNORECASE)


def is_china_stocks_plate(name: str) -> bool:
    return bool(CHINA_STOCKS_PLATE_RE.search(name or ""))


def is_china_plate(name: str) -> bool:
    """China-themed plate by name (hidden in the US-only view). 油气中游 is not one."""
    return is_china_stocks_plate(name) or bool(CHINA_THEME_RE.search(name or ""))


def china_codes(plates: Iterable[dict], members: dict[str, list[str]], extra: Iterable[str] = ()) -> set[str]:
    """China-concept stocks = members of China-concept plates (not mere themes) + user-listed extra codes."""
    out = set(extra or [])
    for p in plates:
        if is_china_stocks_plate(p.get("name", "")):
            out |= set(members.get(p["code"], []))
    return out


def plate_allowed(name: str, plate_members: list[str] | None, china: set[str], min_us: int = 3) -> bool:
    """US-only view: hide China-themed plates and plates made up mostly of China-concept stocks."""
    if is_china_plate(name):
        return False
    if not plate_members:
        return True  # constituents not loaded yet: judged by name only
    n_cn = sum(1 for c in plate_members if c in china)
    if n_cn and (n_cn * 2 >= len(plate_members) or len(plate_members) - n_cn < min_us):
        return False
    return True


def top_bottom(rows: list[dict], key: str, n: int = 10) -> tuple[list[dict], list[dict]]:
    """Up to n rows with the largest positive values and up to n with the most negative values.

    Sign-aware on purpose: a sector that rose must never appear under "largest losses", and a
    sector with net outflow must never appear under "largest inflow".
    """
    valid = [r for r in rows if _num(r.get(key)) is not None]
    desc = sorted(valid, key=lambda r: r[key], reverse=True)
    top = [r for r in desc if r[key] > 0][:n]
    bottom = [r for r in reversed(desc) if r[key] < 0][:n]
    return top, bottom
