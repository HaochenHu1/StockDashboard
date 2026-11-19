"""Mock gateway: runs without OpenD and simulates quotes, option chains, quotas and paper fills.

It deliberately mirrors the OpenD rules the dashboard must respect:
* stock quota 300 (1 per code per subtype), separate option quota 60 (1 per chain per subtype)
* a subscription cannot be cancelled within 60 seconds
* paper trading does not support cancel_all_order (the real API documents the same)
* candles use the bar END time as time_key, in US Eastern time, like OpenD
Debug actions (mock_action) let the UI simulate a lost quote right, an OpenD disconnect and
price moves so every feature can be exercised outside market hours.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
import math
import random
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from ..timeutil import ET, now_et, us_session
from .base import EventSink, Gateway, NotConnectedError, OpenDError, is_option_code, parse_option_code
from .mock_market import MockMarket

log = logging.getLogger(__name__)

BASES = {"US.QQQ": 675.0, "US.SPY": 680.0, "US.IWM": 240.0, "US.DIA": 470.0, "US.NVDA": 190.0,
         "US.AAPL": 270.0, "US.TSLA": 430.0, "US.MSFT": 510.0, "US.AMZN": 240.0, "US.META": 720.0,
         "US.GOOGL": 280.0, "US.AMD": 250.0, "US.UVXY": 12.0, "US.VIXY": 30.0, "US.SOXX": 290.0}
DAILY_EXPIRY = {"US.QQQ", "US.SPY", "US.IWM"}
MARKET_STATE = {"PRE": "PRE_MARKET_BEGIN", "RTH": "MORNING", "AFTER": "AFTER_HOURS_BEGIN",
                "OVERNIGHT": "AFTER_HOURS_END", "CLOSED": "CLOSED"}
SIM_ACC, REAL_ACC = 9000000001, 1000000001
OPEN_STATUSES = {"SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART"}


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black_scholes(S: float, K: float, T: float, r: float, iv: float, is_call: bool) -> dict:
    T = max(T, 1e-5)
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / sq
    d2 = d1 - sq
    if is_call:
        price = S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
        delta = _ncdf(d1)
        theta = (-S * _npdf(d1) * iv / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365
        rho = K * T * math.exp(-r * T) * _ncdf(d2) / 100
    else:
        price = K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1
        theta = (-S * _npdf(d1) * iv / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365
        rho = -K * T * math.exp(-r * T) * _ncdf(-d2) / 100
    gamma = _npdf(d1) / (S * sq)
    vega = S * _npdf(d1) * math.sqrt(T) / 100
    return {"price": max(price, 0.0), "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


@dataclass
class _Px:
    code: str
    price: float
    prev_close: float
    rth_close: float
    open: float
    high: float
    low: float
    volume: float = 0.0
    candles: dict[str, dict] = field(default_factory=dict)   # ktype -> current candle
    history: dict[str, list] = field(default_factory=dict)   # ktype -> completed candles
    ticks: list[dict] = field(default_factory=list)


class MockGateway(Gateway):
    name = "mock"
    is_mock = True
    STOCK_QUOTA = 300
    OPTION_QUOTA = 60
    HOLD_SEC = 60

    def __init__(self, mock_cfg: dict | None = None):
        mock_cfg = mock_cfg or {}
        self.k5_sec = max(10, int(mock_cfg.get("kline_5m_seconds", 300)))
        self.combo_supported = bool(mock_cfg.get("combo_supported", True))
        self._rng = random.Random(20261110)
        self._state = "connecting"
        self._sink: EventSink | None = None
        self._task: asyncio.Task | None = None
        self._px: dict[str, _Px] = {}
        self._subs: dict[tuple[str, str], float] = {}           # (code, subtype) -> subscribed at
        self._lost_right = False
        self._orders: dict[str, dict] = {}
        self._deals: list[dict] = []
        self._positions: dict[tuple[str, int, str], dict] = {}  # (env, acc, code) -> position
        self._seq = itertools.count(1)
        self._chain_cache: dict[str, list[dict]] = {}
        self.market = MockMarket()

    # ------------------------------------------------------------- lifecycle
    async def start(self, sink: EventSink) -> None:
        self._sink = sink
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._state = "disconnected"

    def connection_state(self) -> str:
        return self._state

    def _emit(self, kind: str, payload: Any) -> None:
        if self._sink:
            self._sink(kind, payload)

    async def _run(self) -> None:
        await asyncio.sleep(0.5)
        self._state = "connected"
        self._emit("quote_connected", None)
        self._emit("trade_connected", None)
        while True:
            t0 = time.monotonic()
            try:
                if self._state == "connected":
                    self._tick()
            except Exception:
                log.exception("mock tick failed")
            await asyncio.sleep(max(0.05, 1.0 - (time.monotonic() - t0)))

    def _need(self) -> None:
        if self._state != "connected":
            raise NotConnectedError()

    # ----------------------------------------------------------------- price
    def _base(self, code: str) -> float:
        if code in BASES:
            return BASES[code]
        return float(20 + zlib.crc32(code.encode()) % 400)

    def _p(self, code: str) -> _Px:
        px = self._px.get(code)
        if px is None:
            base = self._base(code)
            prev = round(base / (1 + self.market.drift(code) / 100), 2)
            px = _Px(code, base, prev, base, prev, max(base, prev), min(base, prev), 1_000_000.0)
            self._px[code] = px
        return px

    def _step(self, px: _Px) -> None:
        # prices keep moving even when the market is closed so the UI can be tested at any time
        sigma = 0.25 / math.sqrt(252 * 6.5 * 3600) * 2.0
        px.price = max(0.5, px.price * (1 + self._rng.gauss(0, sigma)))
        px.high, px.low = max(px.high, px.price), min(px.low, px.price)
        vol = float(self._rng.randint(1, 40) * 100)
        px.volume += vol
        now = now_et()
        px.ticks.append({"code": px.code, "time": now.strftime("%Y-%m-%d %H:%M:%S"), "price": round(px.price, 2),
                         "volume": vol, "turnover": round(vol * px.price, 2),
                         "ticker_direction": self._rng.choice(["BUY", "SELL", "NEUTRAL"]), "type": "AUTO_MATCH"})
        px.ticks = px.ticks[-50:]
        for ktype, period in (("K_1M", 60), ("K_5M", self.k5_sec)):
            self._update_candle(px, ktype, period, now)

    def _update_candle(self, px: _Px, ktype: str, period: int, now: dt.datetime) -> None:
        epoch = int(now.timestamp())
        end = (epoch // period + 1) * period
        key = dt.datetime.fromtimestamp(end, ET).strftime("%Y-%m-%d %H:%M:%S")
        c = px.candles.get(ktype)
        p = round(px.price, 2)
        hist = px.history.setdefault(ktype, [])
        if c is None:  # backfill some completed candles so a fresh subscription has history, like OpenD
            back = p
            for i in range(11, 0, -1):
                o = round(back * (1 + self._rng.gauss(0, 0.0008)), 2)
                hk = dt.datetime.fromtimestamp(end - i * period, ET).strftime("%Y-%m-%d %H:%M:%S")
                hist.append({"code": px.code, "time_key": hk, "open": o, "close": back, "high": max(o, back),
                             "low": min(o, back), "volume": 1000.0, "turnover": 0.0, "k_type": ktype, "last_close": o})
                back = o
            hist.reverse()
            for i, h in enumerate(hist):  # time keys ascending after reverse
                h["time_key"] = dt.datetime.fromtimestamp(end - (len(hist) - i) * period, ET).strftime("%Y-%m-%d %H:%M:%S")
        if c is None or c["time_key"] != key:
            if c is not None:
                hist.append(dict(c))
                del hist[:-30]
            last_close = c["close"] if c else p
            c = {"code": px.code, "time_key": key, "open": p, "close": p, "high": p, "low": p,
                 "volume": 0.0, "turnover": 0.0, "k_type": ktype, "last_close": last_close}
            px.candles[ktype] = c
        c["close"] = p
        c["high"], c["low"] = max(c["high"], p), min(c["low"], p)
        c["volume"] += 100.0

    def _stock_record(self, code: str) -> dict:
        px = self._p(code)
        sess = us_session()
        last = px.price if sess in ("RTH", "CLOSED") else px.rth_close
        rec = {"code": code, "name": code.split(".", 1)[1], "update_time": now_et().strftime("%Y-%m-%d %H:%M:%S"),
               "last_price": round(last, 2), "open_price": round(px.open, 2), "high_price": round(px.high, 2),
               "low_price": round(px.low, 2), "prev_close_price": round(px.prev_close, 2), "volume": px.volume,
               "turnover": round(px.volume * px.price, 2), "bid_price": round(px.price - 0.01, 2),
               "ask_price": round(px.price + 0.01, 2), "bid_vol": 300.0, "ask_vol": 500.0,
               "option_valid": False, "sec_status": "NORMAL"}
        mcap = self.market.market_cap(code) * px.price / self._base(code)
        rec["total_market_val"] = round(mcap, 0)
        rec["turnover"] = round(mcap * self.market.turnover_rate(code), 0)
        rec["volume"] = round(rec["turnover"] / max(px.price, 0.01), 0)
        rec["name"] = self.market.names.get(code, rec["name"])
        for pre, s in (("pre", "PRE"), ("after", "AFTER"), ("overnight", "OVERNIGHT")):
            if sess == s:
                rec[f"{pre}_price"] = round(px.price, 2)
                rec[f"{pre}_change_val"] = round(px.price - px.rth_close, 2)
                rec[f"{pre}_change_rate"] = round((px.price / px.rth_close - 1) * 100, 3)
            else:
                rec[f"{pre}_price"] = None
        return rec

    # --------------------------------------------------------------- options
    def _expirations(self, code: str) -> list[str]:
        today = now_et()
        start = today.date() if today.time() < dt.time(16) else today.date() + dt.timedelta(days=1)
        out: set[dt.date] = set()
        d = start
        if code in DAILY_EXPIRY:
            while len(out) < 12:
                if d.weekday() < 5:
                    out.add(d)
                d += dt.timedelta(days=1)
        else:
            while len(out) < 8:
                if d.weekday() == 4:
                    out.add(d)
                d += dt.timedelta(days=1)
        month = dt.date(start.year, start.month, 1)
        for _ in range(7):
            first = month
            third_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
            if third_friday >= start:
                out.add(third_friday)
            month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return [x.isoformat() for x in sorted(out)]

    @staticmethod
    def _strike_step(code: str, s: float) -> float:
        if code in DAILY_EXPIRY:
            return 1.0
        if s < 25:
            return 0.5
        if s < 100:
            return 1.0
        if s < 300:
            return 2.5
        if s < 1000:
            return 5.0
        return 10.0

    def _chain(self, code: str, expiry: str) -> list[dict]:
        key = f"{code}|{expiry}"
        if key in self._chain_cache:
            return self._chain_cache[key]
        s0 = self._base(code)
        step = self._strike_step(code, s0)
        lo, hi = math.floor(s0 * 0.85 / step) * step, math.ceil(s0 * 1.15 / step) * step
        sym = code.split(".", 1)[1]
        ymd = expiry[2:4] + expiry[5:7] + expiry[8:10]
        rows = []
        k = lo
        while k <= hi + 1e-9:
            for cp, typ in (("C", "CALL"), ("P", "PUT")):
                oc = f"US.{sym}{ymd}{cp}{int(round(k * 1000))}"
                rows.append({"code": oc, "name": f"{sym} {ymd} {k:.2f}{cp}", "lot_size": 100, "stock_type": "DRVT",
                             "option_type": typ, "stock_owner": code, "strike_time": expiry, "strike_price": round(k, 3),
                             "suspension": False, "stock_id": zlib.crc32(oc.encode()), "index_option_type": None,
                             "expiration_cycle": "WEEK", "option_standard_type": "STANDARD",
                             "option_settlement_mode": None})
            k = round(k + step, 6)
        self._chain_cache[key] = rows
        return rows

    def _option_quote(self, code: str) -> dict:
        info = parse_option_code(code)
        if info is None:
            raise OpenDError(f"未知合约 {code}")
        S = self._p(info["underlying"]).price
        K = info["strike"]
        expiry_dt = dt.datetime.fromisoformat(info["expiry"] + " 16:00").replace(tzinfo=ET)
        T = max((expiry_dt - now_et()).total_seconds(), 60) / (365 * 86400)
        m = math.log(K / S)
        iv = min(2.0, max(0.08, 0.17 + 0.9 * m * m - 0.12 * m))
        is_call = info["option_type"] == "CALL"
        g = black_scholes(S, K, T, 0.04, iv, is_call)
        theo = g["price"]
        half = max(0.01, round(theo * 0.015, 2))
        bid = max(0.0, round(theo - half, 2))
        ask = max(0.01, round(theo + half, 2))
        h = zlib.crc32(code.encode())
        jitter = ((h >> 4) % 7 - 3) * 0.01
        last = max(0.01, round(theo + jitter, 2))
        return {"code": code, "name": code.split(".", 1)[1], "update_time": now_et().strftime("%Y-%m-%d %H:%M:%S"),
                "last_price": last, "open_price": last, "high_price": last, "low_price": last,
                "prev_close_price": last, "volume": float((h % 5000) + int(time.time() / 60) % 300),
                "turnover": 0.0, "bid_price": bid, "ask_price": ask, "bid_vol": float(h % 90 + 1),
                "ask_vol": float((h >> 3) % 90 + 1), "option_valid": True, "option_type": info["option_type"],
                "strike_time": info["expiry"], "option_strike_price": K, "option_contract_size": 100.0,
                "option_open_interest": int(h % 20000), "option_implied_volatility": round(iv * 100, 3),
                "option_premium": 0.0, "option_delta": round(g["delta"], 5), "option_gamma": round(g["gamma"], 6),
                "option_vega": round(g["vega"], 5), "option_theta": round(g["theta"], 5), "option_rho": round(g["rho"], 5),
                "option_expiry_date_distance": max(0, (dt.date.fromisoformat(info["expiry"]) - now_et().date()).days),
                "option_area_type": "AMERICAN", "option_contract_multiplier": 100.0, "stock_owner": info["underlying"]}

    def _quote_push_record(self, code: str) -> dict:
        if is_option_code(code):
            q = self._option_quote(code)
            return {"code": code, "data_date": q["update_time"][:10], "data_time": q["update_time"][11:],
                    "last_price": q["last_price"], "open_price": q["open_price"], "high_price": q["high_price"],
                    "low_price": q["low_price"], "prev_close_price": q["prev_close_price"], "volume": q["volume"],
                    "open_interest": q["option_open_interest"], "implied_volatility": q["option_implied_volatility"],
                    "delta": q["option_delta"], "gamma": q["option_gamma"], "vega": q["option_vega"],
                    "theta": q["option_theta"], "rho": q["option_rho"], "strike_price": q["option_strike_price"]}
        r = self._stock_record(code)
        out = {k: r.get(k) for k in ("code", "last_price", "open_price", "high_price", "low_price",
                                     "prev_close_price", "volume", "turnover")}
        out["data_date"], out["data_time"] = r["update_time"][:10], r["update_time"][11:]
        for k, v in r.items():
            if k.startswith(("pre_", "after_", "overnight_")):
                out[k] = v
        return out

    def _book(self, code: str) -> dict:
        if is_option_code(code):
            q = self._option_quote(code)
            bid, ask = q["bid_price"], q["ask_price"]
            return {"code": code, "bids": [[bid, q["bid_vol"]]], "asks": [[ask, q["ask_vol"]]]}
        p = self._p(code).price
        return {"code": code,
                "bids": [[round(p - 0.01 * (i + 1), 2), float(100 * self._rng.randint(1, 20))] for i in range(10)],
                "asks": [[round(p + 0.01 * (i + 1), 2), float(100 * self._rng.randint(1, 20))] for i in range(10)]}

    def _bid_ask(self, code: str) -> tuple[float, float]:
        if is_option_code(code):
            q = self._option_quote(code)
            return q["bid_price"], q["ask_price"]
        p = self._p(code).price
        return round(p - 0.01, 2), round(p + 0.01, 2)

    def _mid(self, code: str) -> float:
        if is_option_code(code):
            q = self._option_quote(code)
            return (q["bid_price"] + q["ask_price"]) / 2
        return self._p(code).price

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        for px in list(self._px.values()):
            self._step(px)
        by_type: dict[str, list[str]] = {}
        for (code, st) in self._subs:
            by_type.setdefault(st, []).append(code)
        if by_type.get("QUOTE"):
            self._emit("quote", [self._quote_push_record(c) for c in by_type["QUOTE"]])
        for ktype in ("K_1M", "K_5M"):
            recs = [dict(self._p(c).candles[ktype]) for c in by_type.get(ktype, [])
                    if not is_option_code(c) and ktype in self._p(c).candles]
            if recs:
                self._emit("kline", recs)
        for c in by_type.get("TICKER", []):
            if not is_option_code(c) and self._p(c).ticks:
                self._emit("ticker", [self._p(c).ticks[-1]])
        for c in by_type.get("ORDER_BOOK", []):
            self._emit("orderbook", self._book(c))
        self._fill_orders()

    # ------------------------------------------------------------ quote API
    async def get_global_state(self) -> dict:
        self._need()
        return {"market_us": MARKET_STATE[us_session()], "server_ver": "mock", "qot_logined": True,
                "trd_logined": True, "program_status_type": "READY"}

    async def get_user_info(self) -> dict:
        self._need()
        return {"us_qot_right": "BMP" if self._lost_right else "LV3",
                "us_option_qot_right": "BMP" if self._lost_right else "LV1",
                "sub_quota": self.STOCK_QUOTA, "history_kl_quota": self.STOCK_QUOTA}

    def _quota_use(self, subs: dict[tuple[str, str], float] | None = None) -> tuple[int, int]:
        subs = self._subs if subs is None else subs
        stock = sum(1 for (c, _) in subs if not is_option_code(c))
        chains = {(parse_option_code(c)["underlying"], parse_option_code(c)["expiry"], st)
                  for (c, st) in subs if is_option_code(c)}
        return stock, len(chains)

    async def query_subscription(self) -> dict:
        self._need()
        stock, opt = self._quota_use()
        sub_list: dict[str, list[str]] = {}
        for (c, st) in self._subs:
            sub_list.setdefault(st, []).append(c)
        return {"total_used": stock, "own_used": stock, "remain": self.STOCK_QUOTA - stock,
                "option_used_quota": opt, "option_remain_quota": self.OPTION_QUOTA - opt,
                "own_option_used_quota": opt, "own_security_firm": "N/A", "sub_list": sub_list}

    async def subscribe(self, codes: list[str], subtypes: list[str], session: str | None) -> None:
        self._need()
        trial = dict(self._subs)
        now = time.time()
        for c in codes:
            if c.startswith("US.."):
                raise OpenDError(f"不支持的证券：{c}")
            for st in subtypes:
                trial.setdefault((c, st), now)
        stock, opt = self._quota_use(trial)
        if stock > self.STOCK_QUOTA:
            raise OpenDError("订阅额度不足（股票）")
        if opt > self.OPTION_QUOTA:
            raise OpenDError("订阅额度不足（期权）")
        self._subs = trial
        # first push
        if "QUOTE" in subtypes:
            self._emit("quote", [self._quote_push_record(c) for c in codes])
        for c in codes:
            if not is_option_code(c):
                self._step(self._p(c))
        if "ORDER_BOOK" in subtypes:
            for c in codes:
                self._emit("orderbook", self._book(c))

    async def unsubscribe(self, codes: list[str], subtypes: list[str]) -> None:
        self._need()
        now = time.time()
        for c in codes:
            for st in subtypes:
                t = self._subs.get((c, st))
                if t is not None and now - t < self.HOLD_SEC:
                    raise OpenDError("订阅不足一分钟，不能反订阅")
        for c in codes:
            for st in subtypes:
                self._subs.pop((c, st), None)

    async def unsubscribe_all(self) -> None:
        self._need()
        now = time.time()
        self._subs = {k: t for k, t in self._subs.items() if now - t < self.HOLD_SEC}

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        self._need()
        out = []
        for c in codes:
            if c in ("US.RHHBY", "US.TCEHY", "US.XIACY"):   # like OpenD: one OTC code fails the whole request
                raise OpenDError(f"暂不提供美股 OTC 市场行情 {c.split('.', 1)[1]}")
            if c.startswith("US.."):
                raise OpenDError(f"未知股票 {c}")
            if self.market.is_plate(c):
                out.append(self._plate_record(c))
            else:
                out.append(self._option_quote(c) if is_option_code(c) else self._stock_record(c))
        return out

    def _plate_record(self, code: str) -> dict:
        recs = [self._stock_record(m) for m in self.market.members[code]]
        caps = [r["total_market_val"] for r in recs]
        chg = [(r["last_price"] / r["prev_close_price"] - 1) * 100 for r in recs]
        w = sum(c * x for c, x in zip(caps, chg)) / max(sum(caps), 1)
        name = next(p["plate_name"] for ps in self.market.plates.values() for p in ps if p["code"] == code)
        return {"code": code, "name": name, "update_time": now_et().strftime("%Y-%m-%d %H:%M:%S"),
                "last_price": round(1000 * (1 + w / 100), 3), "prev_close_price": 1000.0,
                "turnover": sum(r["turnover"] for r in recs), "volume": sum(r["volume"] for r in recs),
                "plate_valid": True, "plate_raise_count": sum(1 for x in chg if x > 0),
                "plate_fall_count": sum(1 for x in chg if x < 0), "plate_equal_count": sum(1 for x in chg if x == 0),
                "option_valid": False, "total_market_val": None}

    async def get_cur_kline(self, code: str, num: int, ktype: str) -> list[dict]:
        self._need()
        px = self._p(code)
        self._step(px)
        rows = (px.history.get(ktype, []) + ([px.candles[ktype]] if ktype in px.candles else []))[-num:]
        # like the real SDK: get_cur_kline rows carry no k_type column
        return [{k: v for k, v in c.items() if k != "k_type"} for c in rows]

    async def get_order_book(self, code: str, num: int) -> dict:
        self._need()
        return self._book(code)

    async def get_rt_ticker(self, code: str, num: int) -> list[dict]:
        self._need()
        return list(self._p(code).ticks[-num:])

    async def get_option_expiration_date(self, code: str) -> list[dict]:
        self._need()
        today = now_et().date()
        return [{"strike_time": d, "option_expiry_date_distance": (dt.date.fromisoformat(d) - today).days,
                 "expiration_cycle": "WEEK"} for d in self._expirations(code)]

    async def get_option_chain(self, code: str, start: str, end: str) -> list[dict]:
        self._need()
        rows = []
        for d in self._expirations(code):
            if start <= d <= end:
                rows.extend(self._chain(code, d))
        return rows

    async def get_option_strategy_analysis(self, legs: list[dict]) -> dict:
        self._need()
        bid1 = ask1 = 0.0
        for leg in legs:
            q = self._option_quote(leg["code"])
            r = float(leg.get("ratio", 1))
            if leg["side"] == "BUY":
                bid1 += r * q["bid_price"]
                ask1 += r * q["ask_price"]
            else:
                bid1 -= r * q["ask_price"]
                ask1 -= r * q["bid_price"]
        return {"code": "COMBO", "bid1": round(bid1, 2), "ask1": round(ask1, 2)}

    async def request_highest_quote_right(self) -> str:
        self._need()
        self._lost_right = False
        self._emit("sysnotify", ["QOT_RIGHT", None, {"us_qot_right": "LV3", "us_option_qot_right": "LV1"}])
        return "request_highest_quote_right: success (mock)"

    # --------------------------------------------------- market overview / news
    async def get_plate_list(self, plate_class: str) -> list[dict]:
        self._need()
        if plate_class == "OTHER":
            raise OpenDError("other 不适用于此接口")
        if plate_class == "REGION":
            return []
        if plate_class == "ALL":
            return self.market.plates["INDUSTRY"] + self.market.plates["CONCEPT"]
        return list(self.market.plates[plate_class])

    async def get_plate_stock(self, plate_code: str) -> list[dict]:
        self._need()
        if plate_code not in self.market.members:
            raise OpenDError(f"未知板块 {plate_code}")
        return [{"code": c, "stock_name": self.market.names.get(c, c), "lot_size": 1, "stock_type": "STOCK"}
                for c in self.market.members[plate_code]]

    async def get_capital_flow(self, code: str, period: str = "INTRADAY") -> list[dict]:
        self._need()
        if self.market.is_plate(code):
            raise OpenDError("只支持正股、窝轮和基金，板块不支持")
        r = self._stock_record(code)
        chg = r["last_price"] / r["prev_close_price"] - 1
        noise = ((zlib.crc32(f"{code}{int(time.time() // 300)}".encode()) % 200) - 100) / 100 * 0.03
        flow = r["turnover"] * (chg * 4 + noise)
        t = now_et().strftime("%Y-%m-%d %H:%M:%S")
        return [{"last_valid_time": t, "in_flow": round(flow, 0), "main_in_flow": round(flow * 0.6, 0),
                 "capital_flow_item_time": t}]

    async def get_search_news(self, keyword: str, max_count: int, sub_type: str) -> list[dict]:
        self._need()
        sym = keyword.replace("US.", "")
        today = now_et()
        day = f"{today.year}/{today.month}/{today.day}"
        templates = [
            ("NEWS", "Benzinga", f"{sym} 盘后成交活跃，分析师关注下季度指引"),
            ("NEWS", "MT Newswires", f"{sym} 获得新的大额合同，股价走高"),
            ("NOTICE", "美国SEC公告", f"{sym} | 8-K 重大事件公告"),
        ]
        out = []
        for i, (st, src, title) in enumerate(templates):
            if sub_type not in ("ALL", st):
                continue
            nid = 1004200000 + zlib.crc32(f"{sym}{i}{day}".encode()) % 99999
            kind = "notice" if st == "NOTICE" else "post"
            out.append({"title": title, "news_sub_type": st, "source": src, "publish_time": day, "view_count": 100 + i,
                        "related_securities": [f"US.{sym}"],
                        "url": f"https://www.moomoo.com/hans/news/{kind}/{nid}"})
        return out[:max_count]

    async def get_earnings_calendar(self, begin: str, end: str) -> list[dict]:
        self._need()
        d0, d1 = dt.date.fromisoformat(begin), dt.date.fromisoformat(end)
        if (d1 - d0).days > 7:
            raise OpenDError("日期范围不能超过7天")
        codes = sorted({c for ms in self.market.members.values() for c in ms} | {"US.NVDA", "US.BE", "US.QQQ"})
        rows = []
        for c in codes:
            h = zlib.crc32(c.encode())
            if c not in ("US.NVDA", "US.BE") and h % 4:
                continue
            day = d0 + dt.timedelta(days=h % max(1, (d1 - d0).days + 1))
            pub = ("BEFORE", "AFTER", "REGULAR")[h % 3]
            hour = 7 if pub == "BEFORE" else 16 if pub == "AFTER" else 12
            ts = dt.datetime.combine(day, dt.time(hour, 5), ET).timestamp()
            rows.append({"security": c, "name": self.market.names.get(c, c.split(".", 1)[1]),
                         "earnings_date": day.isoformat(), "earnings_timestamp": ts, "pub_type": pub,
                         "period_text": f"{day.year}Q3", "market_cap": self.market.market_cap(c)})
        return rows

    # ------------------------------------------------ 个股分析: search + F10 (simulated)
    async def get_search_quote(self, keyword: str, max_count: int) -> list[dict]:
        self._need()
        k = keyword.strip().upper()
        names = {**{c: c.split(".", 1)[1] for c in BASES}, **self.market.names}
        out = [{"market": "US", "code": c.split(".", 1)[1], "name": n, "sec_type": "STOCK", "is_watched": False}
               for c, n in sorted(names.items()) if k in c.split(".", 1)[1] or k.lower() in (n or "").lower()]
        return out[:max_count]

    async def get_owner_plate(self, codes: list[str]) -> list[dict]:
        self._need()
        rows = []
        for c in codes:
            for kind, plates in self.market.plates.items():
                for p in plates:
                    if c in self.market.members.get(p["code"], []):
                        rows.append({"code": c, "name": self.market.names.get(c, c), "plate_code": p["code"],
                                     "plate_name": p["plate_name"], "plate_type": kind})
        return rows

    async def get_company_profile(self, code: str) -> list[dict]:
        self._need()
        n = self.market.names.get(code, code.split(".", 1)[1])
        return [{"name": "公司名称", "value": f"{n}（模拟）", "field_type": 1},
                {"name": "所属市场", "value": "NASDAQ", "field_type": 1},
                {"name": "公司简介", "value": "模拟模式下的示例公司简介。", "field_type": 1}]

    async def get_financials_statements(self, code: str, statement_type: int, financial_type: int | None,
                                        num: int) -> dict:
        self._need()
        h = zlib.crc32(code.encode())
        base = 2e9 + h % 50 * 1e9
        names = {1: ["营业收入", "毛利", "营业利润", "净利润"], 2: ["总资产", "总负债", "股东权益"],
                 3: ["经营活动现金流", "资本开支", "自由现金流"], 4: ["毛利率", "净利率", "ROE"]}[statement_type]
        reports = []
        for i in range(min(num, 4)):
            q = 3 - i % 4
            reports.append({"date_time_str": f"2026-{q * 3:02d}-30", "fiscal_year": 2026, "financial_type": "Q",
                            "period_text": f"FY2026 Q{q}（模拟）", "currency_code": "USD",
                            "item_list": [{"field_id": j + 1, "display_name": nm,
                                           "data": round(base * (1 - 0.05 * i) * (0.6 ** j), 0) if statement_type != 4
                                           else round(60 - 12 * j - i, 2), "yoy": round(8 - 2 * i + j, 2)}
                                          for j, nm in enumerate(names)]})
        return {"next_key": "-1", "structure_list": [], "report_list": reports}

    async def get_valuation_detail(self, code: str, valuation_type: int | None, interval_type: int | None) -> dict:
        self._need()
        return {"valuation_type": "PE", "last_update_time_str": now_et().strftime("%Y-%m-%d"),
                "trend": {"current_value": 32.5, "average_value": 28.1, "valuation_percentile": 71.0,
                          "forward_value": 27.4, "historical_items": []}}

    async def get_research_analyst_consensus(self, code: str) -> dict:
        self._need()
        p = self._p(code).price
        return {"highest": round(p * 1.4, 2), "average": round(p * 1.15, 2), "lowest": round(p * 0.8, 2),
                "rating": "BUY", "total": 40, "buy": 30, "hold": 8, "sell": 2}

    async def get_financials_earnings_price_move(self, code: str, period_count: int) -> list[dict]:
        self._need()
        rows = []
        for i in range(min(period_count, 8)):
            move = ((zlib.crc32(f"{code}{i}".encode()) % 140) - 70) / 10
            rows.append({"period_text": f"Q{(3 - i) % 4 + 1}（模拟）", "pub_trading_day_str": f"2026-{10 - i:02d}-20",
                         "pub_type": "AFTER", "day_offset": 1, "trading_day_str": f"2026-{10 - i:02d}-21",
                         "last_close_price": 100.0, "close_price": round(100 + move, 2),
                         "option_iv": 55.0 + i, "option_hv": 40.0 + i})
        return rows

    async def get_option_volatility(self, code: str) -> list[dict]:
        self._need()
        today = now_et().date()
        return [{"timestamp_str": (today - dt.timedelta(days=i)).isoformat(), "implied_volatility": 38.0 + i % 3,
                 "history_volatility": 33.0 + i % 4, "volatility_premium": 5.0} for i in range(10)]

    # ------------------------------------------------------------ trade API
    async def get_acc_list(self) -> list[dict]:
        self._need()
        return [{"acc_id": REAL_ACC, "trd_env": "REAL", "acc_type": "MARGIN", "security_firm": "FUTUINC",
                 "sim_acc_type": None, "trdmarket_auth": ["US"], "acc_status": "ACTIVE"},
                {"acc_id": SIM_ACC, "trd_env": "SIMULATE", "acc_type": "MARGIN", "security_firm": None,
                 "sim_acc_type": "STOCK_AND_OPTION", "trdmarket_auth": ["US"], "acc_status": "ACTIVE"}]

    def _new_order(self, code: str, side: str, qty: float, price: float, env: str, acc_id: int,
                   remark: str, legs: list[dict] | None = None) -> dict:
        if price <= 0 or qty <= 0:
            raise OpenDError("价格和数量必须大于 0")
        if acc_id not in (SIM_ACC, REAL_ACC) or (env == "SIMULATE") != (acc_id == SIM_ACC):
            raise OpenDError("账户与交易环境不匹配")
        oid = f"M{next(self._seq):06d}"
        now = now_et().strftime("%Y-%m-%d %H:%M:%S")
        o = {"order_id": oid, "code": code, "stock_name": code.split(".", 1)[-1], "trd_side": side,
             "order_type": "NORMAL", "order_status": "SUBMITTED", "qty": float(qty), "price": float(price),
             "create_time": now, "updated_time": now, "dealt_qty": 0.0, "dealt_avg_price": 0.0, "last_err_msg": "",
             "remark": remark, "trd_env": env, "acc_id": acc_id, "_legs": legs, "_t": time.time()}
        self._orders[oid] = o
        self._emit("order", [self._pub(o)])
        return o

    @staticmethod
    def _pub(o: dict) -> dict:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    async def place_order(self, *, code: str, side: str, qty: float, price: float, env: str,
                          acc_id: int, remark: str) -> list[dict]:
        self._need()
        if is_option_code(code):
            self._option_quote(code)
        return [self._pub(self._new_order(code, side, qty, price, env, acc_id, remark))]

    async def place_combo_order(self, *, legs: list[dict], qty: float, price: float, env: str,
                                acc_id: int, remark: str) -> list[dict]:
        self._need()
        if not self.combo_supported:
            raise OpenDError("当前账户不支持组合订单")
        code = "+".join(f"{l['side'][0]}{l.get('ratio', 1)}x{l['code']}" for l in legs)
        return [self._pub(self._new_order(code, "BUY", qty, price, env, acc_id, remark, legs))]

    async def comboorder_tradinginfo_query(self, *, legs: list[dict], qty: float, price: float, env: str,
                                           acc_id: int) -> list[dict]:
        self._need()
        if not self.combo_supported:
            raise OpenDError("当前账户不支持组合订单")
        return [{"nlv_change": 0.0, "initial_margin_change": 0.0, "option_bp": 100000.0}]

    def _set_status(self, o: dict, status: str) -> None:
        o["order_status"] = status
        o["updated_time"] = now_et().strftime("%Y-%m-%d %H:%M:%S")
        self._emit("order", [self._pub(o)])

    async def cancel_order(self, *, order_id: str, env: str, acc_id: int) -> list[dict]:
        self._need()
        o = self._orders.get(str(order_id))
        if o is None or o["trd_env"] != env:
            raise OpenDError(f"订单不存在：{order_id}")
        if o["order_status"] not in OPEN_STATUSES:
            raise OpenDError(f"订单已结束（{o['order_status']}），无法撤单")
        self._set_status(o, "CANCELLED_ALL")
        return [{"trd_env": env, "order_id": order_id}]

    async def cancel_all_order(self, *, env: str, acc_id: int) -> list[dict]:
        self._need()
        if env == "SIMULATE":
            raise OpenDError("模拟交易不支持全部撤单")
        for o in self._orders.values():
            if o["trd_env"] == env and o["order_status"] in OPEN_STATUSES:
                self._set_status(o, "CANCELLED_ALL")
        return [{"result": "success"}]

    async def order_list_query(self, *, env: str, acc_id: int, refresh: bool = False) -> list[dict]:
        self._need()
        return [self._pub(o) for o in self._orders.values() if o["trd_env"] == env and o["acc_id"] == acc_id]

    async def deal_list_query(self, *, env: str, acc_id: int) -> list[dict]:
        self._need()
        return [d for d in self._deals if d["trd_env"] == env and d["acc_id"] == acc_id]

    async def position_list_query(self, *, env: str, acc_id: int) -> list[dict]:
        self._need()
        out = []
        for (e, a, code), p in self._positions.items():
            if e == env and a == acc_id and p["qty"] != 0:
                mid = self._mid(code) if not code.count("+") else 0.0
                out.append({"code": code, "qty": p["qty"], "cost_price": p["cost"], "nominal_price": round(mid, 2),
                            "pl_val": round((mid - p["cost"]) * p["qty"] * 100, 2), "trd_env": env})
        return out

    def _fill_orders(self) -> None:
        now = time.time()
        for o in list(self._orders.values()):
            if o["order_status"] not in OPEN_STATUSES or now - o["_t"] < 1.5:
                continue
            try:
                if o["_legs"]:
                    bid = ask = 0.0
                    for leg in o["_legs"]:
                        lb, la = self._bid_ask(leg["code"])
                        r = float(leg.get("ratio", 1))
                        if leg["side"] == "BUY":
                            bid, ask = bid + r * lb, ask + r * la
                        else:
                            bid, ask = bid - r * la, ask - r * lb
                else:
                    bid, ask = self._bid_ask(o["code"])
            except OpenDError:
                continue
            buy = o["trd_side"] in ("BUY", "BUY_BACK")
            # a resting limit inside the spread fills after a short delay (simplified paper fill)
            if (buy and o["price"] + 1e-9 >= bid) or (not buy and o["price"] - 1e-9 <= ask):
                o["dealt_qty"], o["dealt_avg_price"] = o["qty"], o["price"]
                self._set_status(o, "FILLED_ALL")
                deal = {"deal_id": f"D{next(self._seq):06d}", "order_id": o["order_id"], "code": o["code"],
                        "trd_side": o["trd_side"], "qty": o["qty"], "price": o["price"],
                        "create_time": o["updated_time"], "trd_env": o["trd_env"], "acc_id": o["acc_id"]}
                self._deals.append(deal)
                self._emit("deal", [deal])
                key = (o["trd_env"], o["acc_id"], o["code"])
                p = self._positions.setdefault(key, {"qty": 0.0, "cost": 0.0})
                signed = o["qty"] if buy else -o["qty"]
                new_qty = p["qty"] + signed
                if p["qty"] == 0 or (p["qty"] > 0) == (signed > 0):
                    p["cost"] = (p["cost"] * abs(p["qty"]) + o["price"] * abs(signed)) / max(abs(new_qty), 1e-9)
                p["qty"] = new_qty

    # ----------------------------------------------------------- mock debug
    async def mock_action(self, action: str, **kw: Any) -> str:
        if action == "lose_quote_right":
            self._lost_right = True
            self._emit("sysnotify", ["QOT_RIGHT", None, {"us_qot_right": "BMP", "us_option_qot_right": "BMP"}])
            return "已模拟：最高行情权限被其他终端占用"
        if action == "disconnect":
            asyncio.create_task(self._simulate_disconnect(float(kw.get("seconds", 6))))
            return "已模拟：OpenD 断开连接，数秒后自动恢复（订阅会被清空，用于测试自动恢复订阅）"
        if action == "set_price":
            code, price = str(kw["code"]), float(kw["price"])
            if is_option_code(code):
                raise OpenDError("只能设置标的价格")
            px = self._p(code)
            px.price = price
            px.high, px.low = max(px.high, price), min(px.low, price)
            self._step(px)
            return f"已将 {code} 模拟价格设为 {price:g}"
        raise OpenDError(f"未知模拟操作：{action}")

    async def _simulate_disconnect(self, seconds: float) -> None:
        self._state = "disconnected"
        self._emit("quote_disconnected", "mock disconnect")
        self._emit("trade_disconnected", "mock disconnect")
        self._subs.clear()
        await asyncio.sleep(seconds)
        self._state = "connected"
        self._emit("quote_connected", None)
        self._emit("trade_connected", None)
