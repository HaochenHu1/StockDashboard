"""Mock mode only: a simulated analysis built from the simulated quote, fundamentals and option chain.

No API call is made in mock mode (the market data is simulated, so a real model analysis of it would be
meaningless and would cost money). Every text says it is simulated. The trade plans use real contracts of
the simulated chain, so the server-side checks (7 days, chain membership, open interest, spread) run on
them exactly as in live mode.
"""
from __future__ import annotations

from typing import Any

from ..timeutil import now_et

TAG = "（模拟）"


def _pick(contracts: list[dict], right: str, spot: float, offset: int) -> dict | None:
    rows = sorted((c for c in contracts if c["right"] == right and c.get("mid")), key=lambda c: c["strike"])
    if not rows:
        return None
    atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    i = max(0, min(len(rows) - 1, atm + offset))
    return rows[i]


def _leg(side: str, c: dict, expiry: str) -> dict:
    return {"side": side, "right": c["right"], "strike": c["strike"], "expiry": expiry, "bid": c["bid"],
            "ask": c["ask"], "mid": c["mid"], "iv": c["iv"], "open_interest": c["oi"]}


def mock_analysis(market: dict, chain: dict, news: list[dict], peers: list[tuple[str, str]]) -> dict[str, Any]:
    t = market["code"].split(".", 1)[1]
    px = market["price"]["value"] or 100.0
    prev = market["price"].get("prev_close") or px
    bullish = px >= prev
    now = now_et()
    today = now.date().isoformat()
    action, direction = ("买入", "偏多") if bullish else ("卖出", "偏空")
    plans: list[dict] = []
    exps = chain.get("expirations") or []
    common = {"order": f"{TAG}限价单，从组合中间价开始挂单，每 2 分钟向不利方向调整 0.01，最多调整 3 次",
              "take_profit": f"{TAG}组合价值达到最大收益的 50% 时止盈",
              "stop_loss": f"{TAG}组合价值亏损 50% 或标的跌破关键位时止损",
              "latest_exit": f"{TAG}到期日前一个交易日美东 15:30 前离场",
              "invalidation": f"{TAG}标的收盘价反向突破 {px * (0.96 if bullish else 1.04):.2f}",
              "risks": [f"{TAG}到期前时间价值加速衰减（Theta）", f"{TAG}临近到期 Gamma 风险放大，价格小幅波动即可导致盈亏剧烈变化"]}
    if exps:
        e0 = exps[0]
        right = "call" if bullish else "put"
        near = _pick(e0["contracts"], right, px, 0)
        far = _pick(e0["contracts"], right, px, 2 if bullish else -2)
        if near and far and near["strike"] != far["strike"]:
            plans.append({"type": "option", "structure": "Bull Call Spread" if bullish else "Bear Put Spread",
                          "legs": [_leg("buy", near, e0["expiry"]), _leg("sell", far, e0["expiry"])], **common})
        e1 = exps[-1]
        single = _pick(e1["contracts"], right, px, 1 if bullish else -1)
        if single:
            plans.append({"type": "option", "structure": "Long Call" if bullish else "Long Put",
                          "legs": [_leg("buy", single, e1["expiry"])], **common})
    if bullish:
        entry = {"low": round(px * 0.99, 2), "high": round(px * 1.005, 2), "stop": round(px * 0.96, 2),
                 "target": round(px * 1.06, 2), "holding_days": 5}
    else:
        entry = {"low": round(px * 0.995, 2), "high": round(px * 1.01, 2), "stop": round(px * 1.04, 2),
                 "target": round(px * 0.94, 2), "holding_days": 5}
    plans.append({"type": "stock", "structure": "正股" + ("买入" if bullish else "卖空"), "stock_entry": entry,
                  **{**common, "order": f"{TAG}限价单，在买入区间内分两笔挂单",
                     "risks": [f"{TAG}隔夜跳空可能越过止损价"]}})
    fin = market.get("financials") or {}
    metrics = []
    for row in (fin.get("income") or [])[:1]:
        for name, v in list(row["items"].items())[:6]:
            val = v[0] if isinstance(v, list) else v
            metrics.append({"name": name, "value": f"{val:,.0f}" if isinstance(val, (int, float)) else str(val),
                            "period": row.get("period") or "", "source": "moomoo OpenD 财务数据（模拟）"})
    if not metrics:
        metrics = [{"name": "市盈率 TTM", "value": "未取得", "period": "", "source": ""}]
    cat = ["filing", "earnings", "rating", "insider", "legal"]
    news_out = [{"category": cat[i % 5], "date": (n.get("date") or today).replace("/", "-"), "title": n["title"],
                 "summary": f"{TAG}根据 moomoo 资讯标题生成的摘要。", "status": "reported",
                 "price_reaction": f"{TAG}当日股价变动约 {(i % 3) - 1:+d}%",
                 "sources": [{"name": n.get("source") or "moomoo", "url": n["url"]}] if n.get("url") else []}
                for i, n in enumerate(news[:6])]
    return {
        "ticker": t, "generated_at": now.isoformat(),
        "conclusion": {"action": action, "direction_7d": direction,
                       "reasons": [f"{TAG}价格相对昨收{'走强' if bullish else '走弱'}", f"{TAG}期权隐含波动处于中性区间",
                                   f"{TAG}这是模拟模式下生成的示例结论，不是投资建议"],
                       "top_risk": f"{TAG}模拟数据不反映真实市场"},
        "fundamentals": {"metrics": metrics, "earnings_quality_grade": "B",
                         "notes": f"{TAG}模拟模式不调用模型，财报可信度为示例值。"},
        "valuation": {"methods": [
            {"method": "反向 DCF", "fair_value": round(px * 0.95, 2), "assumptions": f"{TAG}隐含 10 年收入复合增速 18%"},
            {"method": "三情景概率加权 DCF", "fair_value": round(px * 1.05, 2), "assumptions": f"{TAG}乐观 / 基准 / 悲观 = 25% / 50% / 25%"},
            {"method": "同业倍数", "fair_value": round(px * 1.02, 2), "assumptions": f"{TAG}同业远期市盈率中位数"}],
            "expectations_gap": f"{TAG}市场隐含增速略高于基准情景。"},
        "news": news_out,
        "gossip": [{"date": today, "title": f"{TAG}社交媒体传闻：{t} 管理层将出席行业大会", "summary": f"{TAG}未经证实。",
                    "status": "rumor", "sources": []}],
        "related_companies": [{"ticker": c.split(".", 1)[1], "name": n, "relation": "competitor",
                               "evidence": f"{TAG}同属一个行业板块", "recent_event": f"{TAG}暂无重大事件",
                               "impact": "neutral"} for c, n in peers[:5]],
        "transmission": f"{TAG}同业的价格战或产能变化会通过估值倍数和订单预期影响本股。",
        "trade_plans": plans,
        "watchlist": [{"metric": f"{TAG}收盘价", "threshold": f"跌破 {px * 0.96:.2f} 时重新评估"},
                      {"metric": f"{TAG}平值隐含波动率", "threshold": "单日上升超过 5 个百分点"}],
        "sources": [{"name": "moomoo OpenD（模拟模式）", "url": "https://openapi.moomoo.com/",
                     "accessed_at": now.isoformat()}],
    }


def mock_delay_steps() -> list[float]:
    """Short pauses so the progress steps are visible in mock mode."""
    return [0.4, 0.4, 0.6]

