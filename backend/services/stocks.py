"""Stock / ETF watchlist: add, remove, per-symbol subscription types, snapshot refresh."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Callable

from ..alerts import normalize_code
from ..gateway.base import STOCK_SUBTYPES, SUBTYPE_ZH, OpenDError, index_hint, is_option_code
from ..ratelimit import Priority
from .market import from_snapshot
from .subscriptions import UserError

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)
STOCK_RE = re.compile(r"^US\.[A-Z][A-Z0-9.\-]{0,9}$")


def validate_stock_code(raw: str) -> str:
    code = normalize_code(raw)
    hint = index_hint(code)
    if hint:
        raise UserError(hint)
    if not code.startswith("US."):
        raise UserError("仅支持美股代码，请使用 moomoo 格式，例如 US.QQQ 或 US.NVDA")
    if is_option_code(code):
        raise UserError("这是期权合约代码，请在“期权”页面固定到期权自选")
    if not STOCK_RE.match(code):
        raise UserError(f"代码格式不正确：{code}。请使用 moomoo 格式，例如 US.QQQ")
    return code


class StockService:
    def __init__(self, app: "AppState"):
        self.app = app
        self.extra_codes: Callable[[], set[str]] = lambda: set()
        self._task: asyncio.Task | None = None

    @property
    def watchlist(self) -> list[dict]:
        return self.app.cfg.get("stocks", "watchlist", default=[]) or []

    def codes(self) -> list[str]:
        return [w["code"] for w in self.watchlist]

    def _save(self, wl: list[dict]) -> None:
        self.app.cfg.set(("stocks", "watchlist"), wl)
        self.app.hub.publish("watchlist", wl)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def restore_subscriptions(self) -> None:
        """Startup: subscribe the configured types for every watchlist symbol."""
        for w in self.watchlist:
            subtypes = [s for s in (w.get("subtypes") or []) if s in STOCK_SUBTYPES]
            if not subtypes:
                continue
            try:
                await self.app.subs.subscribe(w["code"], [w["code"]], subtypes, False)
                await self._seed(w["code"], subtypes)
            except UserError as e:
                log.warning("startup subscribe %s failed: %s", w["code"], e)
                self.app.hub.publish("toast", {"level": "error", "text": f"{w['code']}：{e}"})

    # -------------------------------------------------------------- actions
    async def add(self, raw: str) -> dict:
        code = validate_stock_code(raw)
        if code in self.codes():
            raise UserError(f"{code} 已在自选列表中")
        try:
            recs = await self.app.client.snapshot([code], Priority.NORMAL)
        except OpenDError as e:
            raise UserError(f"无法添加 {code}：{e.msg}") from None
        if not recs:
            raise UserError(f"未找到 {code}")
        self.app.store.update(code, from_snapshot(recs[0]))
        wl = self.watchlist + [{"code": code, "subtypes": []}]
        self._save(wl)
        return {"code": code, "message": f"已添加 {code}"}

    async def remove(self, code: str) -> dict:
        code = normalize_code(code)
        wl = self.watchlist
        item = next((w for w in wl if w["code"] == code), None)
        if item is None:
            raise UserError(f"{code} 不在自选列表中")
        msgs = await self.app.subs.unsubscribe(code, STOCK_SUBTYPES)
        self._save([w for w in wl if w["code"] != code])
        return {"code": code, "messages": msgs}

    async def set_subtype(self, code: str, subtype: str, enabled: bool) -> dict:
        code = normalize_code(code)
        if subtype not in STOCK_SUBTYPES:
            raise UserError(f"不支持的订阅类型：{subtype}")
        wl = self.watchlist
        item = next((w for w in wl if w["code"] == code), None)
        if item is None:
            raise UserError(f"{code} 不在自选列表中")
        current = [s for s in (item.get("subtypes") or []) if s in STOCK_SUBTYPES]
        if enabled:
            msgs = await self.app.subs.subscribe(code, [code], [subtype], False)
            if subtype not in current:
                current.append(subtype)
            await self._seed(code, [subtype])
            msgs.append(f"已订阅 {SUBTYPE_ZH[subtype]}（占用 1 个股票订阅额度）")
        else:
            msgs = await self.app.subs.unsubscribe(code, [subtype])
            current = [s for s in current if s != subtype]
        item["subtypes"] = [s for s in STOCK_SUBTYPES if s in current]
        self._save(wl)
        return {"code": code, "subtypes": item["subtypes"], "messages": msgs}

    async def _seed(self, code: str, subtypes: list[str]) -> None:
        """Fetch initial data right after subscribing (pushes only bring changes)."""
        c, s = self.app.client, self.app.store
        try:
            for st in subtypes:
                if st in ("K_1M", "K_5M"):
                    recs = await c.cur_kline(code, 12, st)
                    for r in recs:  # get_cur_kline has no k_type column (pushes do)
                        r.setdefault("k_type", st)
                        r.setdefault("code", code)
                    s.add_candles(recs)
                elif st == "ORDER_BOOK":
                    s.set_book(await c.order_book(code, 10))
                elif st == "TICKER":
                    s.add_ticks(await c.rt_ticker(code, 20))
        except OpenDError as e:
            log.warning("seed %s %s failed: %s", code, subtypes, e)

    # -------------------------------------------------------------- refresh
    async def _refresh_loop(self) -> None:
        while True:
            interval = float(self.app.cfg.get("stocks", "snapshot_refresh_sec", default=5) or 0)
            if interval <= 0:
                await asyncio.sleep(2)
                continue
            t0 = time.monotonic()
            codes = list(dict.fromkeys(self.codes() + sorted(c for c in self.extra_codes() if not is_option_code(c))))
            if codes and self.app.gw.connection_state() == "connected":
                try:
                    for rec in await self.app.client.snapshot(codes, Priority.POLL):
                        self.app.store.update(rec["code"], from_snapshot(rec))
                except OpenDError as e:
                    log.debug("stock snapshot refresh failed: %s", e)
            await asyncio.sleep(max(0.2, interval - (time.monotonic() - t0)))
