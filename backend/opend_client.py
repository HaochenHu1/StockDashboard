"""OpenDClient: the only way services reach OpenD.

Every call waits for the shared rate limiter first (sliding 30 s window per
endpoint, priority queue), then calls the gateway. It also applies the
per-request size limits from the config: 400 codes per snapshot request and a
30 day span per option chain request.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Awaitable, Callable

from .config import ConfigStore
from .gateway.base import Gateway
from .ratelimit import Priority, SlidingWindowLimiter

log = logging.getLogger(__name__)


class OpenDClient:
    def __init__(self, gateway: Gateway, limiter: SlidingWindowLimiter, cfg: ConfigStore):
        self.gw = gateway
        self.limiter = limiter
        self.cfg = cfg

    async def _call(self, endpoint: str, priority: int, fn: Callable[..., Awaitable[Any]], *a: Any,
                    _consumer: str | None = None, **kw: Any) -> Any:
        await self.limiter.acquire(endpoint, priority, _consumer)
        return await fn(*a, **kw)

    def _spec(self, endpoint: str, key: str, default: Any) -> Any:
        spec = self.cfg.get("rate_limits", endpoint, default={}) or {}
        return spec.get(key, default)

    # ----------------------------------------------------------------- quote
    async def global_state(self, priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_global_state", priority, self.gw.get_global_state)

    async def user_info(self, priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_user_info", priority, self.gw.get_user_info)

    async def query_subscription(self, priority: int = Priority.NORMAL) -> dict:
        return await self._call("query_subscription", priority, self.gw.query_subscription)

    async def subscribe(self, codes: list[str], subtypes: list[str], session: str | None = None,
                        priority: int = Priority.NORMAL) -> None:
        await self._call("subscribe", priority, self.gw.subscribe, codes, subtypes, session)

    async def unsubscribe(self, codes: list[str], subtypes: list[str], priority: int = Priority.NORMAL) -> None:
        await self._call("unsubscribe", priority, self.gw.unsubscribe, codes, subtypes)

    async def unsubscribe_all(self, priority: int = Priority.NORMAL) -> None:
        await self._call("unsubscribe", priority, self.gw.unsubscribe_all)

    async def snapshot(self, codes: list[str], priority: int = Priority.NORMAL,
                       consumer: str | None = None) -> list[dict]:
        codes = list(dict.fromkeys(c for c in codes if c))
        size = max(1, int(self._spec("get_market_snapshot", "max_codes", 400)))
        out: list[dict] = []
        for i in range(0, len(codes), size):
            out.extend(await self._call("get_market_snapshot", priority, self.gw.get_market_snapshot,
                                        codes[i:i + size], _consumer=consumer))
        return out

    # market overview / news -------------------------------------------------
    async def plate_list(self, plate_class: str, priority: int = Priority.BACKGROUND,
                         consumer: str | None = None) -> list[dict]:
        return await self._call("get_plate_list", priority, self.gw.get_plate_list, plate_class, _consumer=consumer)

    async def plate_stock(self, plate_code: str, priority: int = Priority.BACKGROUND,
                          consumer: str | None = None) -> list[dict]:
        return await self._call("get_plate_stock", priority, self.gw.get_plate_stock, plate_code, _consumer=consumer)

    async def capital_flow(self, code: str, period: str = "INTRADAY", priority: int = Priority.BACKGROUND,
                           consumer: str | None = None) -> list[dict]:
        return await self._call("get_capital_flow", priority, self.gw.get_capital_flow, code, period,
                                _consumer=consumer)

    async def search_news(self, keyword: str, max_count: int = 20, sub_type: str = "ALL",
                          priority: int = Priority.BACKGROUND) -> list[dict]:
        return await self._call("get_search_news", priority, self.gw.get_search_news, keyword, max_count, sub_type)

    async def earnings_calendar(self, begin: str, end: str, priority: int = Priority.BACKGROUND) -> list[dict]:
        return await self._call("get_earnings_calendar", priority, self.gw.get_earnings_calendar, begin, end)

    # 个股分析: search + F10 fundamentals ---------------------------------------
    async def search_quote(self, keyword: str, max_count: int = 10, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_search_quote", priority, self.gw.get_search_quote, keyword, max_count)

    async def owner_plate(self, codes: list[str], priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_owner_plate", priority, self.gw.get_owner_plate, codes)

    async def company_profile(self, code: str, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_company_profile", priority, self.gw.get_company_profile, code)

    async def financials_statements(self, code: str, statement_type: int, financial_type: int | None = None,
                                    num: int = 8, priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_financials_statements", priority, self.gw.get_financials_statements,
                                code, statement_type, financial_type, num)

    async def valuation_detail(self, code: str, valuation_type: int | None = None, interval_type: int | None = None,
                               priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_valuation_detail", priority, self.gw.get_valuation_detail,
                                code, valuation_type, interval_type)

    async def analyst_consensus(self, code: str, priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_research_analyst_consensus", priority, self.gw.get_research_analyst_consensus,
                                code)

    async def earnings_price_move(self, code: str, period_count: int = 8, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_financials_earnings_price_move", priority,
                                self.gw.get_financials_earnings_price_move, code, period_count)

    async def option_volatility(self, code: str, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_option_volatility", priority, self.gw.get_option_volatility, code)

    async def cur_kline(self, code: str, num: int, ktype: str, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_cur_kline", priority, self.gw.get_cur_kline, code, num, ktype)

    async def order_book(self, code: str, num: int = 10, priority: int = Priority.NORMAL) -> dict:
        return await self._call("get_order_book", priority, self.gw.get_order_book, code, num)

    async def rt_ticker(self, code: str, num: int = 30, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_rt_ticker", priority, self.gw.get_rt_ticker, code, num)

    async def option_expirations(self, code: str, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_option_expiration_date", priority, self.gw.get_option_expiration_date, code)

    async def option_chain(self, code: str, start: str, end: str, priority: int = Priority.NORMAL) -> list[dict]:
        """Static chain data; spans longer than max_span_days are split into several requests."""
        span = max(1, int(self._spec("get_option_chain", "max_span_days", 30)))
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        out: list[dict] = []
        while d0 <= d1:
            stop = min(d1, d0 + dt.timedelta(days=span - 1))
            out.extend(await self._call("get_option_chain", priority, self.gw.get_option_chain, code,
                                        d0.isoformat(), stop.isoformat()))
            d0 = stop + dt.timedelta(days=1)
        return out

    async def strategy_analysis(self, legs: list[dict], priority: int = Priority.ORDER) -> dict:
        return await self._call("get_option_strategy_analysis", priority, self.gw.get_option_strategy_analysis, legs)

    async def request_highest_quote_right(self, priority: int = Priority.NORMAL) -> str:
        return await self._call("request_highest_quote_right", priority, self.gw.request_highest_quote_right)

    # ----------------------------------------------------------------- trade
    async def acc_list(self, priority: int = Priority.NORMAL) -> list[dict]:
        return await self._call("get_acc_list", priority, self.gw.get_acc_list)

    async def place_order(self, priority: int = Priority.ORDER, **kw: Any) -> list[dict]:
        return await self._call("place_order", priority, self.gw.place_order, **kw)

    async def place_combo_order(self, priority: int = Priority.ORDER, **kw: Any) -> list[dict]:
        return await self._call("place_combo_order", priority, self.gw.place_combo_order, **kw)

    async def combo_trading_info(self, priority: int = Priority.NORMAL, **kw: Any) -> list[dict]:
        return await self._call("comboorder_tradinginfo_query", priority, self.gw.comboorder_tradinginfo_query, **kw)

    async def cancel_order(self, priority: int = Priority.CRITICAL, **kw: Any) -> list[dict]:
        return await self._call("modify_order", priority, self.gw.cancel_order, **kw)

    async def cancel_all_order(self, priority: int = Priority.CRITICAL, **kw: Any) -> list[dict]:
        return await self._call("cancel_all_order", priority, self.gw.cancel_all_order, **kw)

    async def order_list(self, priority: int = Priority.NORMAL, **kw: Any) -> list[dict]:
        return await self._call("order_list_query", priority, self.gw.order_list_query, **kw)

    async def deal_list(self, priority: int = Priority.NORMAL, **kw: Any) -> list[dict]:
        return await self._call("deal_list_query", priority, self.gw.deal_list_query, **kw)

    async def position_list(self, priority: int = Priority.NORMAL, **kw: Any) -> list[dict]:
        return await self._call("position_list_query", priority, self.gw.position_list_query, **kw)
