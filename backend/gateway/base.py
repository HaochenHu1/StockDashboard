"""Gateway interface shared by the real OpenD gateway and the mock gateway.

Services never call a gateway directly; they go through OpenDClient, which
routes every request through the rate limiter.

All methods return plain python data (dicts / lists) with SDK field names, and
raise OpenDError on failure. Push data is delivered to the event sink as
(kind, payload) on the asyncio loop.
"""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

OPTION_CODE_RE = re.compile(r"^US\.([A-Z][A-Z0-9.]*?)(\d{6})([CP])(\d+)$")
US_INDEX_ETF = {
    # moomoo index codes / common index tickers -> suggested ETF
    "VIX": "VIXY 或 UVXY（VIX 期货 ETF，走势与 VIX 指数不完全一致）",
    "NDX": "QQQ", "IXIC": "QQQ", "COMP": "QQQ", "NDX100": "QQQ",
    "SPX": "SPY", "INX": "SPY", "GSPC": "SPY",
    "DJI": "DIA", "DJIA": "DIA", "RUT": "IWM", "SOX": "SOXX", "VXN": "VXN 无对应 ETF，可参考 QQQ 期权隐含波动率",
}

STOCK_SUBTYPES = ["QUOTE", "TICKER", "ORDER_BOOK", "K_1M", "K_5M"]
SUBTYPE_ZH = {"QUOTE": "报价", "TICKER": "逐笔", "ORDER_BOOK": "摆盘", "K_1M": "1 分钟 K 线", "K_5M": "5 分钟 K 线"}

EventSink = Callable[[str, Any], None]


class OpenDError(Exception):
    """An OpenD / SDK call failed. `msg` is the raw message from OpenD."""

    def __init__(self, msg: Any, endpoint: str = ""):
        self.msg = str(msg)
        self.endpoint = endpoint
        super().__init__(f"{endpoint}: {self.msg}" if endpoint else self.msg)


class NotConnectedError(OpenDError):
    def __init__(self, endpoint: str = ""):
        super().__init__("OpenD 未连接", endpoint)


def parse_option_code(code: str) -> dict | None:
    m = OPTION_CODE_RE.match(code or "")
    if not m:
        return None
    sym, ymd, cp, strike = m.groups()
    return {
        "underlying": f"US.{sym}",
        "expiry": f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}",
        "option_type": "CALL" if cp == "C" else "PUT",
        "strike": int(strike) / 1000.0,
    }


def is_option_code(code: str) -> bool:
    return parse_option_code(code) is not None


def index_hint(code: str) -> str | None:
    """Chinese hint when the user enters a US index instead of a tradable symbol."""
    c = (code or "").upper()
    if c.startswith("US.."):
        base = c[4:]
        etf = US_INDEX_ETF.get(base)
        return (f"API 不支持美股指数行情（{c}）。请改用对应的 ETF" + (f"：{etf}。" if etf else "。"))
    if c.startswith("US."):
        base = c[3:].lstrip(".^$")
        if base in US_INDEX_ETF:
            return f"API 不支持美股指数行情（{base} 是指数）。请改用对应的 ETF：{US_INDEX_ETF[base]}。"
    return None


def clean(v: Any) -> Any:
    """SDK values -> JSON-safe python values ('N/A' and NaN become None)."""
    if v is None:
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, str) and v in ("N/A", "n/a", ""):
        return None
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    return v


def df_records(df: Any) -> list[dict]:
    if df is None:
        return []
    if isinstance(df, list):
        return [clean(r) if isinstance(r, dict) else r for r in df]
    try:
        rows = df.to_dict("records")
    except AttributeError:
        return []
    return [{str(k): clean(v) for k, v in r.items()} for r in rows]


class Gateway(ABC):
    name = "base"
    is_mock = False

    @abstractmethod
    async def start(self, sink: EventSink) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def connection_state(self) -> str:
        """'connected' | 'connecting' | 'disconnected'"""

    def trade_ready(self) -> bool:
        return self.connection_state() == "connected"

    # quote
    @abstractmethod
    async def get_global_state(self) -> dict: ...
    @abstractmethod
    async def get_user_info(self) -> dict: ...
    @abstractmethod
    async def query_subscription(self) -> dict: ...
    @abstractmethod
    async def subscribe(self, codes: list[str], subtypes: list[str], session: str | None) -> None: ...
    @abstractmethod
    async def unsubscribe(self, codes: list[str], subtypes: list[str]) -> None: ...
    @abstractmethod
    async def unsubscribe_all(self) -> None: ...
    @abstractmethod
    async def get_market_snapshot(self, codes: list[str]) -> list[dict]: ...
    @abstractmethod
    async def get_cur_kline(self, code: str, num: int, ktype: str) -> list[dict]: ...
    @abstractmethod
    async def get_order_book(self, code: str, num: int) -> dict: ...
    @abstractmethod
    async def get_rt_ticker(self, code: str, num: int) -> list[dict]: ...
    @abstractmethod
    async def get_option_expiration_date(self, code: str) -> list[dict]: ...
    @abstractmethod
    async def get_option_chain(self, code: str, start: str, end: str) -> list[dict]: ...
    @abstractmethod
    async def get_option_strategy_analysis(self, legs: list[dict]) -> dict: ...
    @abstractmethod
    async def request_highest_quote_right(self) -> str: ...

    # trade
    @abstractmethod
    async def get_acc_list(self) -> list[dict]: ...
    @abstractmethod
    async def place_order(self, *, code: str, side: str, qty: float, price: float, env: str,
                          acc_id: int, remark: str) -> list[dict]: ...
    @abstractmethod
    async def place_combo_order(self, *, legs: list[dict], qty: float, price: float, env: str,
                                acc_id: int, remark: str) -> list[dict]: ...
    @abstractmethod
    async def comboorder_tradinginfo_query(self, *, legs: list[dict], qty: float, price: float, env: str,
                                           acc_id: int) -> list[dict]: ...
    @abstractmethod
    async def cancel_order(self, *, order_id: str, env: str, acc_id: int) -> list[dict]: ...
    @abstractmethod
    async def cancel_all_order(self, *, env: str, acc_id: int) -> list[dict]: ...
    @abstractmethod
    async def order_list_query(self, *, env: str, acc_id: int, refresh: bool = False) -> list[dict]: ...
    @abstractmethod
    async def deal_list_query(self, *, env: str, acc_id: int) -> list[dict]: ...
    @abstractmethod
    async def position_list_query(self, *, env: str, acc_id: int) -> list[dict]: ...

    # market overview / news (verified against OpenD 10.12.7208)
    async def get_plate_list(self, plate_class: str) -> list[dict]:
        raise OpenDError("not supported", "get_plate_list")

    async def get_plate_stock(self, plate_code: str) -> list[dict]:
        raise OpenDError("not supported", "get_plate_stock")

    async def get_capital_flow(self, code: str, period: str = "INTRADAY") -> list[dict]:
        raise OpenDError("not supported", "get_capital_flow")

    async def get_search_news(self, keyword: str, max_count: int, sub_type: str) -> list[dict]:
        raise OpenDError("not supported", "get_search_news")

    async def get_earnings_calendar(self, begin: str, end: str) -> list[dict]:
        raise OpenDError("not supported", "get_earnings_calendar")

    # 个股分析: search + F10 fundamentals (SDK 10.12.7208). Return plain dict / list data.
    async def get_search_quote(self, keyword: str, max_count: int) -> list[dict]:
        raise OpenDError("not supported", "get_search_quote")

    async def get_owner_plate(self, codes: list[str]) -> list[dict]:
        raise OpenDError("not supported", "get_owner_plate")

    async def get_company_profile(self, code: str) -> list[dict]:
        raise OpenDError("not supported", "get_company_profile")

    async def get_financials_statements(self, code: str, statement_type: int, financial_type: int | None,
                                        num: int) -> dict:
        raise OpenDError("not supported", "get_financials_statements")

    async def get_valuation_detail(self, code: str, valuation_type: int | None, interval_type: int | None) -> dict:
        raise OpenDError("not supported", "get_valuation_detail")

    async def get_research_analyst_consensus(self, code: str) -> dict:
        raise OpenDError("not supported", "get_research_analyst_consensus")

    async def get_financials_earnings_price_move(self, code: str, period_count: int) -> list[dict]:
        raise OpenDError("not supported", "get_financials_earnings_price_move")

    async def get_option_volatility(self, code: str) -> list[dict]:
        raise OpenDError("not supported", "get_option_volatility")

    # mock-only hooks (no-ops for the real gateway)
    async def mock_action(self, action: str, **kw: Any) -> str:
        raise OpenDError("仅模拟模式可用")
