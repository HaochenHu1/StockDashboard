"""In-memory market data store, normalized across snapshot / quote push / order book / candles."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from ..gateway.base import parse_option_code
from ..timeutil import et_str, us_session
from .hub import Hub

SESSION_PRICE = {"PRE": "pre", "AFTER": "after", "OVERNIGHT": "overnight"}


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def from_snapshot(rec: dict) -> dict:
    out: dict[str, Any] = {
        "name": rec.get("name"), "update_time": rec.get("update_time"),
        "last": _f(rec.get("last_price")), "prev_close": _f(rec.get("prev_close_price")),
        "open": _f(rec.get("open_price")), "high": _f(rec.get("high_price")), "low": _f(rec.get("low_price")),
        "volume": _f(rec.get("volume")), "bid": _f(rec.get("bid_price")), "ask": _f(rec.get("ask_price")),
    }
    for p in ("pre", "after", "overnight"):
        out[f"{p}_price"] = _f(rec.get(f"{p}_price"))
        out[f"{p}_change"] = _f(rec.get(f"{p}_change_val"))
        out[f"{p}_change_pct"] = _f(rec.get(f"{p}_change_rate"))
    if rec.get("option_valid") or parse_option_code(str(rec.get("code", ""))):
        out.update({
            "oi": _f(rec.get("option_open_interest")), "iv": _f(rec.get("option_implied_volatility")),
            "delta": _f(rec.get("option_delta")), "gamma": _f(rec.get("option_gamma")),
            "vega": _f(rec.get("option_vega")), "theta": _f(rec.get("option_theta")),
            "rho": _f(rec.get("option_rho")), "strike": _f(rec.get("option_strike_price")),
            "option_type": rec.get("option_type"), "expiry": rec.get("strike_time"),
            "underlying": rec.get("stock_owner"),
            "multiplier": _f(rec.get("option_contract_multiplier")) or 100.0,
        })
    return out


def from_quote_push(rec: dict) -> dict:
    date, tm = rec.get("data_date"), rec.get("data_time")
    out: dict[str, Any] = {
        "update_time": f"{date} {tm}" if date and tm else None,
        "last": _f(rec.get("last_price")), "prev_close": _f(rec.get("prev_close_price")),
        "open": _f(rec.get("open_price")), "high": _f(rec.get("high_price")), "low": _f(rec.get("low_price")),
        "volume": _f(rec.get("volume")),
    }
    for p in ("pre", "after", "overnight"):
        if f"{p}_price" in rec:
            out[f"{p}_price"] = _f(rec.get(f"{p}_price"))
            out[f"{p}_change"] = _f(rec.get(f"{p}_change_val"))
            out[f"{p}_change_pct"] = _f(rec.get(f"{p}_change_rate"))
    if parse_option_code(str(rec.get("code", ""))):
        out.update({"oi": _f(rec.get("open_interest")), "iv": _f(rec.get("implied_volatility")),
                    "delta": _f(rec.get("delta")), "gamma": _f(rec.get("gamma")), "vega": _f(rec.get("vega")),
                    "theta": _f(rec.get("theta")), "rho": _f(rec.get("rho"))})
    return out


class MarketStore:
    KEEP_CANDLES = 60
    KEEP_TICKS = 40

    def __init__(self, hub: Hub):
        self.hub = hub
        self.data: dict[str, dict[str, Any]] = {}
        self.books: dict[str, dict] = {}
        self.ticks: dict[str, list[dict]] = {}
        self.candles: dict[tuple[str, str], OrderedDict[str, dict]] = {}
        self.changed = asyncio.Event()

    # ------------------------------------------------------------- updates
    def update(self, code: str, fields: dict[str, Any]) -> None:
        if not code:
            return
        d = self.data.setdefault(code, {"code": code})
        info = parse_option_code(code)
        if info and "underlying" not in d:
            d.update({"underlying": info["underlying"], "expiry": info["expiry"],
                      "option_type": info["option_type"], "strike": info["strike"], "multiplier": 100.0})
        changed = False
        for k, v in fields.items():
            if v is None and k not in ("pre_price", "after_price", "overnight_price"):
                continue
            if d.get(k) != v:
                d[k] = v
                changed = True
        if changed:
            self._derive(d)
            self.hub.quote(code, dict(d))
            self.changed.set()

    def set_book(self, book: dict) -> None:
        code = book.get("code")
        if not code:
            return
        self.books[code] = {"bids": book.get("bids", [])[:10], "asks": book.get("asks", [])[:10]}
        bids, asks = book.get("bids") or [], book.get("asks") or []
        self.update(code, {"bid": _f(bids[0][0]) if bids else None, "ask": _f(asks[0][0]) if asks else None,
                           "book_time": et_str()})
        self.hub.publish("book", {"code": code, **self.books[code]})

    def add_ticks(self, recs: list[dict]) -> None:
        touched = set()
        for r in recs:
            code = r.get("code")
            if not code:
                continue
            lst = self.ticks.setdefault(code, [])
            lst.append({k: r.get(k) for k in ("time", "price", "volume", "ticker_direction")})
            del lst[:-self.KEEP_TICKS]
            touched.add(code)
        for code in touched:
            self.hub.publish("ticks", {"code": code, "ticks": self.ticks[code][-15:]})

    def add_candles(self, recs: list[dict]) -> None:
        touched: set[str] = set()
        for r in recs:
            code, ktype, key = r.get("code"), r.get("k_type"), r.get("time_key")
            if not code or not ktype or not key:
                continue
            ktype = str(ktype)
            od = self.candles.setdefault((code, ktype), OrderedDict())
            od[key] = {k: r.get(k) for k in ("time_key", "open", "close", "high", "low", "volume")}
            od.move_to_end(key)
            while len(od) > self.KEEP_CANDLES:
                od.popitem(last=False)
            touched.add(code)
        for code in touched:
            self.refresh_candles(code)

    def refresh_candles(self, code: str | None = None) -> None:
        """Recompute completed / forming candle closes (time_key is the bar END time, ET)."""
        now = et_str()
        codes = [code] if code else list({c for (c, _) in self.candles})
        for c in codes:
            fields: dict[str, Any] = {}
            for ktype, prefix in (("K_5M", "k5"), ("K_1M", "k1")):
                od = self.candles.get((c, ktype))
                if not od:
                    continue
                done = [v for k, v in od.items() if k <= now]
                forming = [v for k, v in od.items() if k > now]
                if done:
                    fields[f"{prefix}_close"] = _f(done[-1]["close"])
                    fields[f"{prefix}_time"] = done[-1]["time_key"]
                if forming:
                    fields[f"{prefix}_forming"] = _f(forming[-1]["close"])
            if fields:
                self.update(c, fields)

    # -------------------------------------------------------------- derive
    @staticmethod
    def _derive(d: dict[str, Any]) -> None:
        last, prev = d.get("last"), d.get("prev_close")
        if last is not None and prev:
            d["change"] = round(last - prev, 4)
            d["change_pct"] = round((last - prev) / prev * 100, 3)
        bid, ask = d.get("bid"), d.get("ask")
        if bid is not None and ask is not None and ask > 0 and 0 <= bid <= ask:
            d["mid"] = round((bid + ask) / 2, 4)
            d["spread"] = round(ask - bid, 4)
        else:
            d["mid"] = d["spread"] = None
        sess = us_session()
        d["session"] = sess
        eff = last
        if "underlying" not in d:  # stocks/ETFs: price of the current extended session if present
            p = SESSION_PRICE.get(sess)
            if p and d.get(f"{p}_price") is not None:
                eff = d[f"{p}_price"]
        d["eff_price"] = eff

    # ---------------------------------------------------------------- read
    def get(self, code: str) -> dict[str, Any] | None:
        return self.data.get(code)

    def alert_view(self, code: str) -> dict[str, Any] | None:
        d = self.data.get(code)
        if not d:
            return None
        return {"last": d.get("eff_price", d.get("last")), "k5_close": d.get("k5_close"),
                "bid": d.get("bid"), "ask": d.get("ask"), "mid": d.get("mid"), "spread": d.get("spread")}

    def detail(self, code: str) -> dict[str, Any]:
        out = {"code": code, "quote": self.data.get(code), "book": self.books.get(code),
               "ticks": self.ticks.get(code, [])[-15:]}
        for ktype in ("K_1M", "K_5M"):
            od = self.candles.get((code, ktype))
            out[ktype] = list(od.values())[-6:] if od else []
        return out
