"""Real gateway: one OpenQuoteContext + one OpenSecTradeContext to the local OpenD.

* SDK calls are blocking, so they run in two separate thread pools. Quote work
  (snapshot polling etc.) can never occupy the threads that send orders.
* Push callbacks arrive on SDK threads and are handed to the asyncio loop.
* The SDK reconnects by itself and re-subscribes what it knows about; the
  SubscriptionManager additionally verifies and restores subscriptions after a
  reconnect event.
* Trading is never unlocked from code. Live trading must be unlocked by hand
  in the OpenD GUI.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from moomoo import (RET_OK, ComboLeg, CurKlineHandlerBase, KLType, Market, ModifyOrderOp, NewsSubType,
                    OpenQuoteContext, OpenSecTradeContext, OrderBookHandlerBase, OrderType, PeriodType, Plate,
                    SecurityFirm, Session,
                    StockQuoteHandlerBase, SubType, SysNotifyHandlerBase, TickerHandlerBase, TimeInForce,
                    TradeDealHandlerBase, TradeOrderHandlerBase, TrdEnv, TrdMarket, TrdSide, UserInfoField)
from moomoo.common.constant import ContextStatus, StrategyLegAction
from moomoo.quote.quote_query import OptionStrategyLeg

from .base import EventSink, Gateway, NotConnectedError, OpenDError, clean, df_records

log = logging.getLogger(__name__)


def norm_book(d: Any) -> dict:
    if not isinstance(d, dict):
        return {"code": None, "bids": [], "asks": []}
    def side(rows):
        out = []
        for r in rows or []:
            try:
                out.append([clean(r[0]), clean(r[1])])
            except (IndexError, TypeError):
                continue
        return out
    return {"code": d.get("code"), "bids": side(d.get("Bid")), "asks": side(d.get("Ask"))}


class _QuoteCtx(OpenQuoteContext):
    def __init__(self, gw: "MoomooGateway", **kw: Any):
        self._gw = gw                      # set before super(): async connect may call back early
        super().__init__(**kw)

    def on_api_socket_reconnected(self):
        ret = super().on_api_socket_reconnected()
        self._gw._emit("quote_connected", None)
        return ret

    def on_disconnect(self, conn_id, reason, msg):
        super().on_disconnect(conn_id, reason, msg)
        self._gw._emit("quote_disconnected", str(msg))


class _TradeCtx(OpenSecTradeContext):
    def __init__(self, gw: "MoomooGateway", **kw: Any):
        self._gw = gw
        super().__init__(**kw)

    def on_api_socket_reconnected(self):
        ret = super().on_api_socket_reconnected()
        if self._gw._t is self:  # the first connect happens inside __init__; the gateway announces that one itself
            self._gw._emit("trade_connected", None)
        return ret

    def on_disconnect(self, conn_id, reason, msg):
        super().on_disconnect(conn_id, reason, msg)
        self._gw._emit("trade_disconnected", str(msg))


def _handler(base: type, kind: str, convert: Callable[[Any], Any]):
    class H(base):  # type: ignore[misc, valid-type]
        def __init__(self, gw: "MoomooGateway"):
            super().__init__()
            self._gw = gw

        def on_recv_rsp(self, rsp_pb):
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret == RET_OK:
                try:
                    self._gw._emit(kind, convert(data))
                except Exception:  # never let a push error kill the SDK thread
                    log.exception("push handler %s failed", kind)
            return ret, data
    H.__name__ = f"{kind.title()}Handler"
    return H


QuoteH = _handler(StockQuoteHandlerBase, "quote", df_records)
KlineH = _handler(CurKlineHandlerBase, "kline", df_records)
TickerH = _handler(TickerHandlerBase, "ticker", df_records)
BookH = _handler(OrderBookHandlerBase, "orderbook", norm_book)
SysH = _handler(SysNotifyHandlerBase, "sysnotify", clean)
OrderH = _handler(TradeOrderHandlerBase, "order", df_records)
DealH = _handler(TradeDealHandlerBase, "deal", df_records)


class MoomooGateway(Gateway):
    name = "moomoo"
    is_mock = False

    def __init__(self, opend_cfg: dict):
        self.host = str(opend_cfg.get("host", "127.0.0.1"))
        self.port = int(opend_cfg.get("port", 11111))
        self.telnet_port = int(opend_cfg.get("telnet_port", 22222))
        self.security_firm = str(opend_cfg.get("security_firm", "FUTUINC"))
        self.ai_type = int(opend_cfg.get("ai_type", 1))
        self._q: _QuoteCtx | None = None
        self._t: _TradeCtx | None = None
        self._qexec = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opend-quote")
        self._texec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="opend-trade")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sink: EventSink | None = None
        self._trade_task: asyncio.Task | None = None
        self._stopping = False

    # ---------------------------------------------------------------- events
    def _emit(self, kind: str, payload: Any) -> None:
        loop, sink = self._loop, self._sink
        if loop is None or sink is None or loop.is_closed() or self._stopping:
            return
        try:
            loop.call_soon_threadsafe(sink, kind, payload)
        except RuntimeError:
            pass  # loop shutting down

    # ------------------------------------------------------------- lifecycle
    async def start(self, sink: EventSink) -> None:
        self._loop = asyncio.get_running_loop()
        self._sink = sink

        def make_quote() -> _QuoteCtx:
            ctx = _QuoteCtx(self, host=self.host, port=self.port, is_async_connect=True, ai_type=self.ai_type)
            for h in (QuoteH, KlineH, TickerH, BookH, SysH):
                ctx.set_handler(h(self))
            return ctx

        self._q = await self._loop.run_in_executor(self._qexec, make_quote)
        self._trade_task = asyncio.create_task(self._trade_connect_loop())
        log.info("quote context created for %s:%s (async connect)", self.host, self.port)

    async def _trade_connect_loop(self) -> None:
        # OpenSecTradeContext connects synchronously and retries forever, so only build it once
        # the quote connection proves OpenD is reachable.
        while not self._stopping and self._t is None:
            if self.connection_state() == "connected":
                try:
                    def make_trade() -> _TradeCtx:
                        ctx = _TradeCtx(self, filter_trdmarket=TrdMarket.US, host=self.host, port=self.port,
                                        security_firm=getattr(SecurityFirm, self.security_firm),
                                        ai_type=self.ai_type)
                        ctx.set_handler(OrderH(self))
                        ctx.set_handler(DealH(self))
                        return ctx
                    self._t = await self._loop.run_in_executor(self._texec, make_trade)
                    log.info("trade context ready (security_firm=%s)", self.security_firm)
                    self._emit("trade_connected", None)
                except Exception:
                    log.exception("creating trade context failed; retrying")
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        if self._trade_task:
            self._trade_task.cancel()
        self._sink = None  # no more push dispatch during shutdown
        loop = asyncio.get_running_loop()
        for ctx, ex in ((self._t, self._texec), (self._q, self._qexec)):
            if ctx is not None:
                try:
                    await asyncio.wait_for(loop.run_in_executor(ex, ctx.close), timeout=5)
                except Exception:
                    log.exception("closing context failed")
        self._stopping = True
        self._qexec.shutdown(wait=False, cancel_futures=True)
        self._texec.shutdown(wait=False, cancel_futures=True)

    def connection_state(self) -> str:
        if self._q is None:
            return "connecting"
        st = self._q.status
        if st == ContextStatus.READY:
            return "connected"
        if st in (ContextStatus.START, ContextStatus.CONNECTING, ContextStatus.CONNECTED):
            return "connecting"
        return "disconnected"

    def trade_ready(self) -> bool:
        return self._t is not None and self._t.status == ContextStatus.READY

    # ---------------------------------------------------------------- calls
    async def _call(self, ex: ThreadPoolExecutor, ctx: Any, endpoint: str, fn: Callable, *a: Any, **kw: Any) -> Any:
        if ctx is None or ctx.status != ContextStatus.READY:
            raise NotConnectedError(endpoint)
        loop = asyncio.get_running_loop()
        ret, data = await loop.run_in_executor(ex, functools.partial(fn, *a, **kw))
        if ret != RET_OK:
            raise OpenDError(data, endpoint)
        return data

    async def _q_call(self, endpoint: str, *a: Any, **kw: Any) -> Any:
        ctx = self._q
        return await self._call(self._qexec, ctx, endpoint, getattr(ctx, endpoint), *a, **kw)

    async def _t_call(self, endpoint: str, *a: Any, **kw: Any) -> Any:
        ctx = self._t
        if ctx is None:
            raise NotConnectedError(endpoint)
        return await self._call(self._texec, ctx, endpoint, getattr(ctx, endpoint), *a, **kw)

    # ----------------------------------------------------------------- quote
    async def get_global_state(self) -> dict:
        return clean(await self._q_call("get_global_state"))

    async def get_user_info(self) -> dict:
        return clean(await self._q_call("get_user_info", info_field=[UserInfoField.QOTRIGHT, UserInfoField.API]))

    async def query_subscription(self) -> dict:
        return clean(await self._q_call("query_subscription", is_all_conn=True))

    async def subscribe(self, codes: list[str], subtypes: list[str], session: str | None) -> None:
        kw: dict[str, Any] = {"is_first_push": True, "subscribe_push": True}
        if session:
            kw["session"] = getattr(Session, session)
        await self._q_call("subscribe", codes, [getattr(SubType, s) for s in subtypes], **kw)

    async def unsubscribe(self, codes: list[str], subtypes: list[str]) -> None:
        await self._q_call("unsubscribe", codes, [getattr(SubType, s) for s in subtypes])

    async def unsubscribe_all(self) -> None:
        await self._q_call("unsubscribe_all")

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        return df_records(await self._q_call("get_market_snapshot", codes))

    async def get_cur_kline(self, code: str, num: int, ktype: str) -> list[dict]:
        return df_records(await self._q_call("get_cur_kline", code, num, getattr(KLType, ktype)))

    async def get_order_book(self, code: str, num: int) -> dict:
        return norm_book(await self._q_call("get_order_book", code, num=num))

    async def get_rt_ticker(self, code: str, num: int) -> list[dict]:
        return df_records(await self._q_call("get_rt_ticker", code, num=num))

    async def get_option_expiration_date(self, code: str) -> list[dict]:
        return df_records(await self._q_call("get_option_expiration_date", code))

    async def get_option_chain(self, code: str, start: str, end: str) -> list[dict]:
        return df_records(await self._q_call("get_option_chain", code, start=start, end=end))

    async def get_option_strategy_analysis(self, legs: list[dict]) -> dict:
        objs = []
        for leg in legs:
            o = OptionStrategyLeg()
            o.code = leg["code"]
            o.action = StrategyLegAction.BUY if leg["side"] == "BUY" else StrategyLegAction.SELL
            o.quantity = float(leg.get("ratio", 1))
            objs.append(o)
        rows = df_records(await self._q_call("get_option_strategy_analysis", objs))
        return rows[0] if rows else {}

    async def request_highest_quote_right(self) -> str:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.telnet_port), timeout=3)
        except (OSError, asyncio.TimeoutError) as e:
            raise OpenDError(f"无法连接 OpenD Telnet 端口 {self.host}:{self.telnet_port}（{e.__class__.__name__}）。"
                             f"请在 OpenD 设置中开启 Telnet（地址 127.0.0.1，端口 {self.telnet_port}）后重试。",
                             "request_highest_quote_right") from None
        try:
            writer.write(b"request_highest_quote_right\r\n")
            await writer.drain()
            chunks = []
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=1.5)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                chunks.append(data)
            raw = b"".join(chunks)
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc).strip()
                except UnicodeDecodeError:
                    continue
            return raw.decode("latin-1").strip()
        finally:
            writer.close()

    # --------------------------------------------------- market overview / news
    async def get_plate_list(self, plate_class: str) -> list[dict]:
        return df_records(await self._q_call("get_plate_list", Market.US, getattr(Plate, plate_class)))

    async def get_plate_stock(self, plate_code: str) -> list[dict]:
        return df_records(await self._q_call("get_plate_stock", plate_code))

    async def get_capital_flow(self, code: str, period: str = "INTRADAY") -> list[dict]:
        return df_records(await self._q_call("get_capital_flow", code, period_type=getattr(PeriodType, period)))

    async def get_search_news(self, keyword: str, max_count: int, sub_type: str) -> list[dict]:
        return df_records(await self._q_call("get_search_news", keyword, max_count=max_count,
                                             news_sub_type=getattr(NewsSubType, sub_type)))

    async def get_earnings_calendar(self, begin: str, end: str) -> list[dict]:
        return df_records(await self._q_call("get_earnings_calendar", Market.US, begin_date=begin, end_date=end))

    # ------------------------------------------------ 个股分析: search + F10
    @staticmethod
    def _plain(data: Any) -> Any:
        """DataFrame -> records; dict / list -> JSON-safe values (the F10 calls return either)."""
        return df_records(data) if hasattr(data, "to_dict") and not isinstance(data, dict) else clean(data)

    async def get_search_quote(self, keyword: str, max_count: int) -> list[dict]:
        return df_records(await self._q_call("get_search_quote", keyword, max_count=max_count))

    async def get_owner_plate(self, codes: list[str]) -> list[dict]:
        return df_records(await self._q_call("get_owner_plate", codes))

    async def get_company_profile(self, code: str) -> list[dict]:
        return df_records(await self._q_call("get_company_profile", code))

    async def get_financials_statements(self, code: str, statement_type: int, financial_type: int | None,
                                        num: int) -> dict:
        return self._plain(await self._q_call("get_financials_statements", code, statement_type=statement_type,
                                              financial_type=financial_type, num=num))

    async def get_valuation_detail(self, code: str, valuation_type: int | None, interval_type: int | None) -> dict:
        return self._plain(await self._q_call("get_valuation_detail", code, valuation_type=valuation_type,
                                              interval_type=interval_type))

    async def get_research_analyst_consensus(self, code: str) -> dict:
        return self._plain(await self._q_call("get_research_analyst_consensus", code))

    async def get_financials_earnings_price_move(self, code: str, period_count: int) -> list[dict]:
        return df_records(await self._q_call("get_financials_earnings_price_move", code, period_count=period_count))

    async def get_option_volatility(self, code: str) -> list[dict]:
        return df_records(await self._q_call("get_option_volatility", code))

    # ----------------------------------------------------------------- trade
    async def get_acc_list(self) -> list[dict]:
        return df_records(await self._t_call("get_acc_list"))

    async def place_order(self, *, code: str, side: str, qty: float, price: float, env: str,
                          acc_id: int, remark: str) -> list[dict]:
        return df_records(await self._t_call(
            "place_order", price=price, qty=qty, code=code, trd_side=getattr(TrdSide, side),
            order_type=OrderType.NORMAL, trd_env=getattr(TrdEnv, env), acc_id=acc_id,
            remark=remark, time_in_force=TimeInForce.DAY))

    @staticmethod
    def _combo_legs(legs: list[dict]) -> list[ComboLeg]:
        out = []
        for leg in legs:
            c = ComboLeg()
            c.code = leg["code"]
            c.trd_side = getattr(TrdSide, leg["side"])
            c.qty_ratio = float(leg.get("ratio", 1))
            out.append(c)
        return out

    async def place_combo_order(self, *, legs: list[dict], qty: float, price: float, env: str,
                                acc_id: int, remark: str) -> list[dict]:
        return df_records(await self._t_call(
            "place_combo_order", combo_leg_list=self._combo_legs(legs), price=price, qty=qty,
            order_type=OrderType.NORMAL, trd_env=getattr(TrdEnv, env), acc_id=acc_id, remark=remark,
            time_in_force=TimeInForce.DAY))

    async def comboorder_tradinginfo_query(self, *, legs: list[dict], qty: float, price: float, env: str,
                                           acc_id: int) -> list[dict]:
        return df_records(await self._t_call(
            "comboorder_tradinginfo_query", combo_leg_list=self._combo_legs(legs), price=price, qty=qty,
            order_type=OrderType.NORMAL, trd_env=getattr(TrdEnv, env), acc_id=acc_id))

    async def cancel_order(self, *, order_id: str, env: str, acc_id: int) -> list[dict]:
        return df_records(await self._t_call(
            "modify_order", ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=getattr(TrdEnv, env), acc_id=acc_id))

    async def cancel_all_order(self, *, env: str, acc_id: int) -> list[dict]:
        data = await self._t_call("cancel_all_order", trd_env=getattr(TrdEnv, env), acc_id=acc_id,
                                  trdmarket=TrdMarket.US)
        return df_records(data) if not isinstance(data, (str, type(None))) else [{"result": clean(data)}]

    async def order_list_query(self, *, env: str, acc_id: int, refresh: bool = False) -> list[dict]:
        return df_records(await self._t_call("order_list_query", trd_env=getattr(TrdEnv, env), acc_id=acc_id,
                                             refresh_cache=refresh))

    async def deal_list_query(self, *, env: str, acc_id: int) -> list[dict]:
        return df_records(await self._t_call("deal_list_query", trd_env=getattr(TrdEnv, env), acc_id=acc_id))

    async def position_list_query(self, *, env: str, acc_id: int) -> list[dict]:
        return df_records(await self._t_call("position_list_query", trd_env=getattr(TrdEnv, env), acc_id=acc_id))
