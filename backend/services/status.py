"""Status panel: OpenD connection, quote rights, quota, rate-limit usage, lost-quote-right detection."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..gateway.base import OpenDError
from ..ratelimit import Priority
from ..timeutil import us_session

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)
RIGHT_RANK = {None: 0, "N/A": 0, "NO": 0, "BMP": 1, "LV1": 2, "LV2": 3, "LV3": 4, "SF": 5}


class StatusService:
    def __init__(self, app: "AppState"):
        self.app = app
        self.global_state: dict[str, Any] = {}
        self.rights: dict[str, Any] = {}
        self.best: dict[str, int] = {}
        self.right_lost = False
        self.right_lost_since: float | None = None
        self.last_request: dict | None = None
        self.was_connected = False
        self._task: asyncio.Task | None = None
        self._t_state = self._t_rights = self._t_quota = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def view(self) -> dict:
        return {
            "mode": "mock" if self.app.gw.is_mock else "opend",
            "connection": self.app.gw.connection_state(),
            "trade_ready": self.app.gw.trade_ready(),
            "market_us": self.global_state.get("market_us"),
            "qot_logined": self.global_state.get("qot_logined"),
            "trd_logined": self.global_state.get("trd_logined"),
            "server_ver": self.global_state.get("server_ver"),
            "session": us_session(),
            "us_qot_right": self.rights.get("us_qot_right"),
            "us_option_qot_right": self.rights.get("us_option_qot_right"),
            "right_lost": self.right_lost,
            "last_request": self.last_request,
            "quota": self.app.subs.quota_view(),
            "rate": self.app.limiter.usage(),
            "env": self.app.trading.env,
            "auto_enabled": self.app.trading.auto_enabled,
            "server_time": time.time(),
            "news_sources": self.app.news.sources_view() if hasattr(self.app, "news") else [],
        }

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._poll()
            except Exception:
                log.exception("status poll failed")
            self.app.hub.publish("status", self.view())

    async def _poll(self) -> None:
        if self.app.gw.connection_state() != "connected":
            return
        now = time.monotonic()
        if now - self._t_state >= 5:
            self._t_state = now
            try:
                self.global_state = await self.app.client.global_state(Priority.NORMAL)
            except OpenDError as e:
                log.debug("global_state failed: %s", e)
        if now - self._t_rights >= 15:
            self._t_rights = now
            await self.refresh_rights()
        if now - self._t_quota >= 10:
            self._t_quota = now
            try:
                await self.app.subs.refresh_quota()
            except OpenDError:
                pass

    async def refresh_rights(self) -> None:
        try:
            info = await self.app.client.user_info(Priority.NORMAL)
        except OpenDError as e:
            log.debug("user_info failed: %s", e)
            return
        self.apply_rights(info)

    def apply_rights(self, info: dict) -> None:
        self.rights = {k: info.get(k) for k in ("us_qot_right", "us_option_qot_right", "sub_quota")}
        lost = False
        for k in ("us_qot_right", "us_option_qot_right"):
            rank = RIGHT_RANK.get(self.rights.get(k), 0)
            self.best[k] = max(self.best.get(k, 0), rank)
            if rank < self.best[k]:
                lost = True
        if lost and not self.right_lost:
            self.right_lost_since = time.time()
            log.warning("highest quote right lost: %s", self.rights)
            self.app.hub.publish("toast", {"level": "error",
                                           "text": "最高行情权限已被其他终端占用（例如手机上的 moomoo App），行情可能降级"})
        if not lost and self.right_lost:
            self.app.hub.publish("toast", {"level": "info", "text": "已恢复最高行情权限"})
        self.right_lost = lost

    def on_sysnotify(self, content: Any) -> None:
        log.info("sys notify: %s", content)
        text = str(content)
        if "QOT_RIGHT" in text:
            if isinstance(content, (list, tuple)) and len(content) >= 3 and isinstance(content[2], dict):
                merged = {**self.rights, **{k: v for k, v in content[2].items() if k in ("us_qot_right", "us_option_qot_right")}}
                self.apply_rights(merged)
            asyncio.create_task(self.refresh_rights())
        if "QUOTA" in text:
            asyncio.create_task(self._quota_soon())

    async def _quota_soon(self) -> None:
        try:
            await self.app.subs.refresh_quota()
        except OpenDError:
            pass

    async def request_quote_right(self) -> dict:
        try:
            text = await self.app.client.request_highest_quote_right(Priority.NORMAL)
            ok = True
        except OpenDError as e:
            text, ok = e.msg, False
        self.last_request = {"ok": ok, "text": text, "ts": time.time()}
        log.info("request_highest_quote_right -> ok=%s %s", ok, text)
        await asyncio.sleep(1.5)
        await self.refresh_rights()
        self.app.hub.publish("status", self.view())
        return {"ok": ok, "text": text, "right_lost": self.right_lost}
