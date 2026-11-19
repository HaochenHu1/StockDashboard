"""Steps 1-3 of POST /api/stock-analysis: quote + fundamentals, the option chain (<= 7 calendar days), news.

Every OpenD call goes through OpenDClient (shared rate limiter, UI priority). A failing F10 call never
stops the analysis: the gap is recorded and handed to the model and the UI as 「未取得」.
The option chain is real data only (moomoo get_option_chain + option snapshots); nothing is simulated.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, Awaitable

from ..errors import UserError
from ..gateway.base import OpenDError
from ..news.sources import is_non_us
from ..ratelimit import Priority
from ..timeutil import ET, now_et, us_session

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "name", "update_time", "last_price", "prev_close_price", "open_price", "high_price", "low_price", "volume",
    "turnover", "turnover_rate", "amplitude", "total_market_val", "circular_market_val", "issued_shares",
    "outstanding_shares", "pe_ratio", "pe_ttm_ratio", "pb_ratio", "ey_ratio", "earning_per_share",
    "net_asset_per_share", "net_profit", "net_asset", "dividend_ttm", "dividend_ratio_ttm", "dividend_lfy",
    "dividend_lfy_ratio", "highest52weeks_price", "lowest52weeks_price", "highest_history_price",
    "lowest_history_price", "pre_price", "pre_change_rate", "after_price", "after_change_rate",
    "overnight_price", "overnight_change_rate", "sec_status")
SESSION_LABEL = {"PRE": ("pre", "盘前价"), "AFTER": ("after", "盘后价"), "OVERNIGHT": ("overnight", "夜盘价")}


def et_iso(text: Any) -> str:
    """OpenD 'YYYY-MM-DD HH:MM:SS' (US Eastern) -> ISO 8601 with offset."""
    try:
        return dt.datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).isoformat()
    except (TypeError, ValueError):
        return ""


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


async def _try(label: str, aw: Awaitable[Any], gaps: list[str]) -> Any:
    try:
        return await aw
    except OpenDError as e:
        gaps.append(f"{label}：{e.msg}")
    except Exception as e:  # never let one F10 call stop the analysis
        log.exception("stock analysis: %s failed", label)
        gaps.append(f"{label}：{e.__class__.__name__}")
    return None


# --------------------------------------------------------------- compaction
def _statement(data: Any, max_items: int = 45) -> list[dict] | None:
    """F10 statement dict -> [{period, date, currency, items: {name: value | [value, yoy%]}}]"""
    if not isinstance(data, dict):
        return None
    out = []
    for r in data.get("report_list") or []:
        items: dict[str, Any] = {}
        for it in (r.get("item_list") or [])[:max_items]:
            name = it.get("display_name") or str(it.get("field_id"))
            if it.get("data") is None:
                continue
            items[name] = [it["data"], it["yoy"]] if it.get("yoy") is not None else it["data"]
        out.append({"period": r.get("period_text") or r.get("date_time_str"), "date": r.get("date_time_str"),
                    "currency": r.get("currency_code") or r.get("currency_info"), "items": items})
    return out or None


def _valuation(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    out = {k: v for k, v in data.items() if k not in ("trend",)}
    trend = data.get("trend")
    if isinstance(trend, dict):
        t = {k: v for k, v in trend.items() if k != "historical_items"}
        t["recent_history"] = (trend.get("historical_items") or [])[-12:]
        out["trend"] = t
    return out


def _earnings_moves(rows: Any) -> list[dict] | None:
    if not isinstance(rows, list) or not rows:
        return None
    keep = ("period_text", "pub_trading_day_str", "pub_type", "day_offset", "trading_day_str", "open_price",
            "close_price", "highest_price", "lowest_price", "last_close_price", "option_iv", "option_hv")
    return [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows[:120]]


# ------------------------------------------------------------------ step 1
async def collect_market(app: "AppState", code: str) -> dict:
    """Quote + fundamentals. Raises UserError only if the quote itself cannot be fetched."""
    gaps: list[str] = []
    try:
        recs = await app.client.snapshot([code], Priority.NORMAL)
    except OpenDError as e:
        raise UserError(f"获取 {code} 行情失败：{e.msg}") from None
    if not recs:
        raise UserError(f"OpenD 没有返回 {code} 的行情")
    rec = recs[0]
    sess = us_session()
    value, label = _num(rec.get("last_price")), "最新价 / 收盘价"
    if sess in SESSION_LABEL:
        key, lab = SESSION_LABEL[sess]
        if _num(rec.get(f"{key}_price")):
            value, label = _num(rec.get(f"{key}_price")), lab
    as_of = et_iso(rec.get("update_time"))
    price = {"value": value, "as_of": as_of, "source": f"moomoo OpenD 快照（{label}）", "session": sess,
             "regular_last": _num(rec.get("last_price")), "prev_close": _num(rec.get("prev_close_price"))}
    quote = {k: rec.get(k) for k in SNAPSHOT_FIELDS if rec.get(k) is not None}

    plates = await _try("所属行业", app.client.owner_plate([code]), gaps)
    industry = [p.get("plate_name") for p in plates or [] if str(p.get("plate_type", "")).upper() == "INDUSTRY"]
    concepts = [p.get("plate_name") for p in plates or [] if str(p.get("plate_type", "")).upper() != "INDUSTRY"]
    profile = await _try("公司概况", app.client.company_profile(code), gaps)
    profile = [{"name": r.get("name"), "value": str(r.get("value") or "")[:600]}
               for r in profile or [] if r.get("name")][:30] or None

    fin: dict[str, Any] = {}
    for key, label2, st, num in (("income", "利润表", 1, 8), ("balance_sheet", "资产负债表", 2, 4),
                                 ("cash_flow", "现金流量表", 3, 4), ("key_metrics", "主要指标", 4, 8)):
        rows = _statement(await _try(label2, app.client.financials_statements(code, st, None, num), gaps))
        if rows is None and not any(g.startswith(label2) for g in gaps):
            gaps.append(f"{label2}：OpenD 未返回数据")
        fin[key] = rows
    valuation = _valuation(await _try("估值（PE 等）", app.client.valuation_detail(code), gaps))
    consensus = await _try("分析师一致预期", app.client.analyst_consensus(code), gaps)
    moves = _earnings_moves(await _try("历次财报前后股价与波动率", app.client.earnings_price_move(code, 8), gaps))
    vol = await _try("隐含 / 历史波动率", app.client.option_volatility(code), gaps)
    vol = vol[-30:] if isinstance(vol, list) and vol else None
    name = rec.get("name") or code
    return {"code": code, "name": name, "price": price, "quote": quote,
            "company": {"name": name, "market_cap": _num(rec.get("total_market_val")),
                        "industry": industry, "concepts": concepts[:8], "profile": profile},
            "financials": fin, "valuation": valuation, "analyst_consensus": consensus,
            "earnings_price_moves": moves, "volatility": vol, "gaps": gaps}


# ------------------------------------------------------------------ step 2
async def collect_chain(app: "AppState", code: str, spot: float | None, max_days: int = 7,
                        window_pct: float = 20.0, max_each_side: int = 30) -> dict:
    """Expirations within max_days calendar days (US Eastern), strikes near the money, with quotes.
    Each contract: strike, expiry, bid, ask, mid, IV (percent), open interest, today's volume."""
    now = now_et()
    today = now.date()
    try:
        exps = await app.options.get_expirations(code)
    except UserError:
        raise
    except OpenDError as e:
        raise UserError(f"获取 {code} 期权到期日失败：{e.msg}") from None
    keep = []
    for e in exps:
        try:
            days = (dt.date.fromisoformat(e["date"]) - today).days
        except (KeyError, ValueError):
            continue
        if 0 <= days <= max_days and not (days == 0 and now.time() >= dt.time(16, 0)):
            keep.append((e["date"], days))
    out: dict[str, Any] = {"underlying": code, "spot": spot, "fetched_at": now.isoformat(), "max_days": max_days,
                           "latest_allowed_expiry": (today + dt.timedelta(days=max_days)).isoformat(),
                           "all_expirations": [e.get("date") for e in exps][:12], "expirations": []}
    picked: list[tuple[str, int, list[dict]]] = []
    for d, days in keep:
        ch = await app.options.ensure_chain(code, d)
        rows = [r for r in ch.get("strikes") or [] if r.get("call") or r.get("put")]
        if spot:
            rows = [r for r in rows if abs(r["strike"] / spot - 1) * 100 <= window_pct] or rows
            rows.sort(key=lambda r: r["strike"])
            atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot)) if rows else 0
            rows = rows[max(0, atm - max_each_side): atm + max_each_side + 1]
        picked.append((d, days, rows))
    codes = [c for _, _, rows in picked for r in rows for c in (r.get("call"), r.get("put")) if c]
    quotes: dict[str, dict] = {}
    if codes:
        try:
            for r in await app.client.snapshot(codes, Priority.NORMAL):
                quotes[r.get("code")] = r
        except OpenDError as e:
            raise UserError(f"获取期权报价失败：{e.msg}") from None
    for d, days, rows in picked:
        contracts = []
        for r in rows:
            for right in ("call", "put"):
                oc = r.get(right)
                if not oc:
                    continue
                q = quotes.get(oc) or {}
                bid, ask = _num(q.get("bid_price")), _num(q.get("ask_price"))
                mid = round((bid + ask) / 2, 4) if bid is not None and ask and ask > 0 and 0 <= bid <= ask else None
                oi = _num(q.get("option_open_interest"))
                contracts.append({"code": oc, "right": right, "strike": round(float(r["strike"]), 3),
                                  "bid": bid, "ask": ask, "mid": mid, "iv": _num(q.get("option_implied_volatility")),
                                  "oi": int(oi) if oi is not None else None, "volume": _num(q.get("volume")),
                                  "last": _num(q.get("last_price"))})
        out["expirations"].append({"expiry": d, "dte": days, "contracts": contracts, "atm": _atm(contracts, spot)})
    if not keep:
        out["note"] = f"{max_days} 个自然日内没有到期的期权合约"
    return out


def _atm(contracts: list[dict], spot: float | None) -> dict | None:
    """At-the-money straddle priced at mid, and the move it implies."""
    if not spot:
        return None
    by_strike: dict[float, dict] = {}
    for c in contracts:
        by_strike.setdefault(c["strike"], {})[c["right"]] = c
    both = [k for k, v in by_strike.items() if v.get("call", {}).get("mid") and v.get("put", {}).get("mid")]
    if not both:
        return None
    k = min(both, key=lambda x: abs(x - spot))
    straddle = by_strike[k]["call"]["mid"] + by_strike[k]["put"]["mid"]
    return {"strike": k, "straddle_mid": round(straddle, 4), "implied_move_pct": round(straddle / spot * 100, 2),
            "call_iv": by_strike[k]["call"].get("iv"), "put_iv": by_strike[k]["put"].get("iv")}


def chain_for_model(chain: dict) -> dict:
    """Same chain without contract codes (the model needs strike / expiry / right and the quotes)."""
    return {**{k: v for k, v in chain.items() if k != "expirations"},
            "units": "bid / ask / mid / last 为每股美元价格（每张合约 100 股）；iv 为百分比；oi 为未平仓张数；volume 为当日成交张数",
            "expirations": [{"expiry": e["expiry"], "dte": e["dte"], "atm_straddle": e["atm"],
                             "contracts": [{k: c[k] for k in ("right", "strike", "bid", "ask", "mid", "iv", "oi", "volume")}
                                           for c in e["contracts"]]} for e in chain.get("expirations") or []]}


# ------------------------------------------------------------------ step 3
async def collect_news(app: "AppState", code: str, name: str | None) -> tuple[list[dict], list[str]]:
    """moomoo news and announcements for the symbol (OpenD get_search_news), US-related only."""
    gaps: list[str] = []
    ticker = code.split(".", 1)[1]
    items: list[dict] = []
    for sub, label in (("NEWS", "moomoo 资讯"), ("NOTICE", "moomoo 公告")):
        rows = await _try(label, app.client.search_news(ticker, 20, sub, Priority.NORMAL), gaps)
        for r in rows or []:
            title = str(r.get("title") or "").strip()
            if not title or is_non_us(title):
                continue
            items.append({"type": sub.lower(), "title": title[:300], "source": r.get("source"),
                          "date": str(r.get("publish_time") or "")[:10], "url": r.get("url")})
    return items[:30], gaps
