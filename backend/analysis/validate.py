"""Pure functions: parse the model's JSON, normalize it, and check trade plans in code.

Trade plan rules (prompt 1.4) are enforced here, never left to the model:
1. an option leg that expires more than 7 calendar days from today (US Eastern) removes the plan
2. an option leg whose strike + expiry + right is not in the chain we fetched removes the plan
3. an option leg with open interest < 500 or (ask - bid) > 10 % of mid removes the plan
4. break-even points, max profit / max loss per contract and the capital or margin needed are
   recomputed from each leg's mid price and replace anything the model wrote
5. a "观望" conclusion shows no trade plan at all
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any

ACTIONS = ("买入", "卖出", "观望")
DIRECTIONS = ("偏多", "偏空", "方向不明")
GRADES = ("A", "B", "C", "D")
NEWS_CATEGORIES = ("filing", "earnings", "rating", "insider", "legal")
STATUSES = ("confirmed", "reported", "rumor")
RELATIONS = ("supplier", "customer", "competitor", "substitute", "partner")
IMPACTS = ("positive", "negative", "neutral")
MISSING = "未取得"


# ------------------------------------------------------------------ parsing
def extract_json(text: str) -> dict | None:
    """The model is asked for JSON only; tolerate code fences or a stray sentence around it."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    dec = json.JSONDecoder()
    best: dict | None = None
    best_len = 0
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            v, end = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(v, dict) and end - i > best_len:
            best, best_len = v, end - i
            if "conclusion" in v:
                break
    return best


def structure_problems(d: dict | None) -> list[str]:
    """Missing / malformed top-level parts that justify one retry."""
    if not isinstance(d, dict):
        return ["返回内容不是 JSON 对象"]
    out = []
    c = d.get("conclusion")
    if not isinstance(c, dict):
        out.append("缺少 conclusion 对象")
    elif _choice(c.get("action"), ACTIONS) is None:
        out.append("conclusion.action 必须是 买入 / 卖出 / 观望 之一")
    for key, kind in (("fundamentals", dict), ("valuation", dict), ("news", list), ("gossip", list),
                      ("related_companies", list), ("trade_plans", list), ("watchlist", list), ("sources", list)):
        if key in d and not isinstance(d[key], kind):
            out.append(f"{key} 的类型应为 {'对象' if kind is dict else '数组'}")
    return out


# ------------------------------------------------------------- normalizing
def _s(v: Any, limit: int = 2000) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    return str(v).strip()[:limit]


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.replace(",", "").replace("$", "").strip()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _choice(v: Any, allowed: tuple[str, ...]) -> str | None:
    s = _s(v, 40)
    if s in allowed:
        return s
    low = s.lower()
    for a in allowed:
        if a.lower() == low:
            return a
    return None


def _url(v: Any) -> str:
    s = _s(v, 1000)
    return s if re.match(r"^https?://", s, re.I) else ""


def _date(v: Any) -> str:
    s = _s(v, 40)
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else s[:10]


def _sources(v: Any) -> list[dict]:
    out = []
    for s in v if isinstance(v, list) else []:
        if isinstance(s, dict):
            url = _url(s.get("url"))
            name = _s(s.get("name"), 200)
            if url or name:
                out.append({"name": name or url, "url": url})
        elif isinstance(s, str) and _url(s):
            out.append({"name": s, "url": s})
    return out[:8]


def _strs(v: Any, n: int = 10, limit: int = 600) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [_s(x, limit) for x in (v if isinstance(v, list) else []) if _s(x)][:n]


def normalize(d: dict) -> dict:
    """Shape the model output into the 1.3 structure; unknown values become '' / None so the UI shows 未取得."""
    c = d.get("conclusion") if isinstance(d.get("conclusion"), dict) else {}
    f = d.get("fundamentals") if isinstance(d.get("fundamentals"), dict) else {}
    val = d.get("valuation") if isinstance(d.get("valuation"), dict) else {}
    out: dict[str, Any] = {
        "conclusion": {"action": _choice(c.get("action"), ACTIONS) or "观望",
                       "direction_7d": _choice(c.get("direction_7d"), DIRECTIONS) or "方向不明",
                       "reasons": _strs(c.get("reasons"), 6), "top_risk": _s(c.get("top_risk"), 800)},
        "fundamentals": {
            "metrics": [{"name": _s(m.get("name"), 120), "value": _s(m.get("value"), 120),
                         "period": _s(m.get("period"), 80), "source": _s(m.get("source"), 200)}
                        for m in (f.get("metrics") if isinstance(f.get("metrics"), list) else [])
                        if isinstance(m, dict) and _s(m.get("name"))][:30],
            "earnings_quality_grade": _choice(f.get("earnings_quality_grade"), GRADES),
            "notes": _s(f.get("notes"), 2000)},
        "valuation": {
            "methods": [{"method": _s(m.get("method"), 120), "fair_value": _f(m.get("fair_value")),
                         "assumptions": _s(m.get("assumptions"), 1200)}
                        for m in (val.get("methods") if isinstance(val.get("methods"), list) else [])
                        if isinstance(m, dict) and _s(m.get("method"))][:6],
            "expectations_gap": _s(val.get("expectations_gap"), 2000)},
        "news": [], "gossip": [], "related_companies": [],
        "transmission": _s(d.get("transmission"), 3000),
        "trade_plans": [p for p in (d.get("trade_plans") if isinstance(d.get("trade_plans"), list) else [])
                        if isinstance(p, dict)][:3],
        "watchlist": [{"metric": _s(w.get("metric"), 200), "threshold": _s(w.get("threshold"), 300)}
                      for w in (d.get("watchlist") if isinstance(d.get("watchlist"), list) else [])
                      if isinstance(w, dict) and _s(w.get("metric"))][:12],
        "sources": [],
    }
    for n in d.get("news") if isinstance(d.get("news"), list) else []:
        if not isinstance(n, dict) or not _s(n.get("title")):
            continue
        out["news"].append({"category": _choice(n.get("category"), NEWS_CATEGORIES) or "filing",
                            "date": _date(n.get("date")), "title": _s(n.get("title"), 300),
                            "summary": _s(n.get("summary"), 1200),
                            "status": _choice(n.get("status"), STATUSES) or "rumor",
                            "price_reaction": _s(n.get("price_reaction"), 300),
                            "sources": _sources(n.get("sources"))})
    for n in d.get("gossip") if isinstance(d.get("gossip"), list) else []:
        if not isinstance(n, dict) or not _s(n.get("title")):
            continue
        out["gossip"].append({"date": _date(n.get("date")), "title": _s(n.get("title"), 300),
                              "summary": _s(n.get("summary"), 1200),
                              "status": _choice(n.get("status"), STATUSES) or "rumor",
                              "sources": _sources(n.get("sources"))})
    for r in d.get("related_companies") if isinstance(d.get("related_companies"), list) else []:
        if not isinstance(r, dict) or not (_s(r.get("ticker")) or _s(r.get("name"))):
            continue
        out["related_companies"].append({
            "ticker": _s(r.get("ticker"), 20).upper().replace("US.", ""), "name": _s(r.get("name"), 120),
            "relation": _choice(r.get("relation"), RELATIONS) or "partner", "evidence": _s(r.get("evidence"), 600),
            "recent_event": _s(r.get("recent_event"), 600), "impact": _choice(r.get("impact"), IMPACTS) or "neutral"})
    out["related_companies"] = out["related_companies"][:15]
    for s in d.get("sources") if isinstance(d.get("sources"), list) else []:
        if isinstance(s, dict) and (_url(s.get("url")) or _s(s.get("name"))):
            out["sources"].append({"name": _s(s.get("name"), 200) or _url(s.get("url")), "url": _url(s.get("url")),
                                   "accessed_at": _s(s.get("accessed_at"), 40)})
    out["sources"] = out["sources"][:60]
    return out


# --------------------------------------------------------------- the chain
def strike_key(v: Any) -> float | None:
    f = _f(v)
    return round(f, 3) if f is not None else None


def chain_index(chain: dict) -> dict[tuple[str, str, float], dict]:
    """chain = {"expirations": [{"expiry": d, "contracts": [{right, strike, bid, ask, mid, iv, oi, volume, code}]}]}"""
    idx = {}
    for e in chain.get("expirations") or []:
        for c in e.get("contracts") or []:
            k = strike_key(c.get("strike"))
            if k is not None:
                idx[(e["expiry"], c["right"], k)] = c
    return idx


# ------------------------------------------------------------------ payoff
def _intrinsic(right: str, strike: float, s: float) -> float:
    return max(s - strike, 0.0) if right == "call" else max(strike - s, 0.0)


def payoff(legs: list[dict], spot: float | None, multiplier: float = 100.0) -> dict:
    """Expiry P&L of one unit of the structure, priced at each leg's mid.

    Piecewise linear in the underlying price S with kinks at the strikes, so the extremes are at
    S = 0, at a strike, or at S -> infinity (slope = net long calls)."""
    sign = {"buy": 1.0, "sell": -1.0}
    net_debit = sum(sign[l["side"]] * l["mid"] for l in legs)            # per share; < 0 = credit received
    strikes = sorted({l["strike"] for l in legs})
    pts = [0.0] + strikes

    def pnl(s: float) -> float:
        return (sum(sign[l["side"]] * _intrinsic(l["right"], l["strike"], s) for l in legs) - net_debit) * multiplier

    vals = [pnl(x) for x in pts]
    slope = sum(sign[l["side"]] for l in legs if l["right"] == "call") * multiplier  # P&L per $1 above top strike
    max_profit = None if slope > 1e-9 else max(vals)
    max_loss = None if slope < -1e-9 else min(vals)
    bes: list[float] = []
    for (x0, v0), (x1, v1) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
        if v0 == 0:
            bes.append(x0)
        elif v0 * v1 < 0:
            bes.append(x0 + (x1 - x0) * (-v0) / (v1 - v0))
    if vals[-1] == 0 and pts[-1] > 0:
        bes.append(pts[-1])
    elif abs(slope) > 1e-9 and vals[-1] * slope < 0:
        bes.append(pts[-1] - vals[-1] / slope)
    bes = sorted({round(b, 2) for b in bes if b > 0})

    shorts = [l for l in legs if l["side"] == "sell"]
    if not shorts:
        capital, note = net_debit * multiplier, "买入期权：支付的权利金即所需资金"
    elif len(legs) == 1 and legs[0]["right"] == "put":
        l0 = legs[0]
        capital = l0["strike"] * multiplier
        note = (f"现金担保：行权价 × {multiplier:g} = {capital:,.0f} 美元；"
                f"收到权利金 {l0['mid'] * multiplier:,.0f} 美元后净占用 {capital - l0['mid'] * multiplier:,.0f} 美元")
    elif max_loss is not None:
        capital = max(-max_loss, net_debit * multiplier, 0.0)
        note = "有限风险组合：保证金按最大亏损估算（以券商实际要求为准）"
    else:
        capital = _reg_t_naked(legs, spot, multiplier)
        note = ("最大亏损无上限；保证金按 Reg T 裸卖公式估算（以券商实际要求为准）" if capital is not None
                else "最大亏损无上限；缺少标的价格，无法估算保证金")
    return {"net_premium": round(net_debit * multiplier, 2),          # > 0 pay (debit), < 0 receive (credit)
            "limit_price": round(net_debit, 2),                        # per-share combo price to start at
            "breakevens": bes,
            "max_profit": None if max_profit is None else round(max_profit, 2),
            "max_loss": None if max_loss is None else round(max_loss, 2),   # negative number = loss
            "unlimited_profit": max_profit is None, "unlimited_loss": max_loss is None,
            "capital": None if capital is None else round(capital, 2), "capital_note": note,
            "multiplier": multiplier}


def _reg_t_naked(legs: list[dict], spot: float | None, mult: float) -> float | None:
    """Reg T estimate for short legs not covered by a long leg of the same right:
    premium + max(20 % of S - OTM amount, 10 % of S [call] / 10 % of strike [put])."""
    if not spot:
        return None
    total = 0.0
    for right in ("call", "put"):
        longs = sorted(l["strike"] for l in legs if l["right"] == right and l["side"] == "buy")
        shorts = sorted((l for l in legs if l["right"] == right and l["side"] == "sell"), key=lambda l: l["strike"])
        uncovered = shorts[len(longs):] if right == "call" else shorts[:max(0, len(shorts) - len(longs))]
        for l in uncovered:
            otm = max(l["strike"] - spot, 0) if right == "call" else max(spot - l["strike"], 0)
            floor = 0.1 * (spot if right == "call" else l["strike"])
            total += (l["mid"] + max(0.2 * spot - otm, floor)) * mult
    return total


# ------------------------------------------------------------ trade plans
def _dte(expiry: str, today: dt.date) -> int | None:
    try:
        return (dt.date.fromisoformat(expiry) - today).days
    except ValueError:
        return None


def check_trade_plans(plans: list[dict], action: str, chain: dict, today: dt.date, spot: float | None,
                      max_days: int = 7, min_oi: int = 500, max_spread_pct: float = 10.0) -> tuple[list[dict], list[dict]]:
    """Returns (kept plans with recomputed numbers, removed plans with reasons in Chinese)."""
    if action == "观望":
        removed = [{"structure": _s(p.get("structure"), 120) or "未命名方案",
                    "reasons": ["结论为“观望”，不显示交易方案"]} for p in plans]
        return [], removed
    idx = chain_index(chain)
    kept, removed = [], []
    for p in plans[:3]:
        name = _s(p.get("structure"), 120) or "未命名方案"
        typ = _choice(p.get("type"), ("stock", "option")) or ("option" if p.get("legs") else "stock")
        base = {"type": typ, "structure": name, "order": _s(p.get("order"), 800),
                "take_profit": _s(p.get("take_profit"), 800), "stop_loss": _s(p.get("stop_loss"), 800),
                "latest_exit": _s(p.get("latest_exit"), 400), "invalidation": _s(p.get("invalidation"), 800),
                "risks": _strs(p.get("risks"), 8, 500)}
        if typ == "stock":
            plan, reasons = _check_stock(p, base)
        else:
            plan, reasons = _check_option(p, base, idx, today, spot, max_days, min_oi, max_spread_pct)
        if reasons:
            removed.append({"structure": name, "reasons": reasons})
        else:
            kept.append(plan)
    return kept, removed


def _check_stock(p: dict, base: dict) -> tuple[dict, list[str]]:
    e = p.get("stock_entry") if isinstance(p.get("stock_entry"), dict) else {}
    low, high, stop, target = (_f(e.get(k)) for k in ("low", "high", "stop", "target"))
    days = _f(e.get("holding_days"))
    if None in (low, high, stop, target) or min(low, high, stop, target) <= 0:
        return {}, ["正股方案缺少有效的买入区间、止损价或目标价"]
    low, high = min(low, high), max(low, high)
    if stop < low and target > high:
        side, worst, risk = "long", high, high - stop
    elif stop > high and target < low:
        side, worst, risk = "short", low, stop - low
    else:
        return {}, ["正股方案的止损价和目标价不在买入区间两侧"]
    mid = (low + high) / 2
    reward = (target - mid) if side == "long" else (mid - target)
    return {**base, "stock_entry": {"low": low, "high": high, "stop": stop, "target": target,
                                    "holding_days": int(days) if days else None},
            "computed": {"side": side, "worst_entry": round(worst, 2), "max_loss_per_share": round(risk, 2),
                         "reward_per_share": round(reward, 2),
                         "reward_risk": round(reward / risk, 2) if risk > 0 else None}}, []


def _check_option(p: dict, base: dict, idx: dict, today: dt.date, spot: float | None, max_days: int,
                  min_oi: int, max_spread_pct: float) -> tuple[dict, list[str]]:
    raw_legs = [l for l in (p.get("legs") if isinstance(p.get("legs"), list) else []) if isinstance(l, dict)]
    if not raw_legs:
        return {}, ["期权方案没有任何期权腿"]
    if len(raw_legs) > 4:
        return {}, ["期权方案超过 4 条腿，看板不支持"]
    reasons: list[str] = []
    legs = []
    for i, l in enumerate(raw_legs, 1):
        side = _choice(l.get("side"), ("buy", "sell"))
        right = _choice(l.get("right"), ("call", "put"))
        strike = strike_key(l.get("strike"))
        expiry = _date(l.get("expiry"))
        tag = f"第 {i} 腿 {('买入' if side == 'buy' else '卖出') if side else '?'} {expiry} {strike if strike is not None else '?'} {right or '?'}"
        if side is None or right is None or strike is None:
            reasons.append(f"{tag}：方向、类型或行权价无效")
            continue
        days = _dte(expiry, today)
        if days is None:
            reasons.append(f"{tag}：到期日格式无效")
            continue
        if days < 0:
            reasons.append(f"{tag}：已过期")
            continue
        if days > max_days:
            reasons.append(f"{tag}：到期日距今天 {days} 个自然日，超过 {max_days} 天")
            continue
        c = idx.get((expiry, right, strike))
        if c is None:
            reasons.append(f"{tag}：期权链中没有这个合约（行权价与到期日不匹配）")
            continue
        oi, bid, ask, mid = c.get("oi"), c.get("bid"), c.get("ask"), c.get("mid")
        if oi is None or oi < min_oi:
            reasons.append(f"{tag}：未平仓量 {'未取得' if oi is None else int(oi)} 张，低于 {min_oi} 张")
            continue
        if bid is None or ask is None or mid is None or mid <= 0:
            reasons.append(f"{tag}：缺少有效买卖价，无法计算价差")
            continue
        spread_pct = (ask - bid) / mid * 100
        if spread_pct > max_spread_pct + 1e-9:
            reasons.append(f"{tag}：买卖价差 {ask - bid:.2f} 为中间价的 {spread_pct:.1f}%，超过 {max_spread_pct:g}%")
            continue
        legs.append({"side": side, "right": right, "strike": strike, "expiry": expiry, "code": c.get("code"),
                     "bid": bid, "ask": ask, "mid": mid, "iv": c.get("iv"), "open_interest": oi,
                     "volume": c.get("volume"), "dte": days, "spread_pct": round(spread_pct, 1)})
    if reasons:
        return {}, reasons
    return {**base, "legs": legs, "computed": payoff(legs, spot)}, []
