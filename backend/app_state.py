"""Wires config, rate limiter, gateway, client and services together."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .alerts import CONDITION_TYPES
from .analysis.service import StockAnalysisService
from .config import ConfigStore
from .gateway.base import STOCK_SUBTYPES, SUBTYPE_ZH, Gateway, is_option_code
from .opend_client import OpenDClient
from .ratelimit import SlidingWindowLimiter
from .market.service import MarketService
from .news.service import NewsService
from .services.alerts_service import AlertService
from .services.hub import Hub
from .services.market import MarketStore, from_quote_push
from .services.options import OptionService
from .services.status import StatusService
from .services.stocks import StockService
from .services.subscriptions import SubscriptionManager
from .services.trading import TradingService

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, config_path: Path, mock: bool, base_dir: Path, gateway: Gateway | None = None,
                 data_root: Path | None = None):
        self.base_dir = base_dir
        self.mock = mock
        root = data_root or base_dir
        self.data_dir = root / "data" / ("mock" if mock else "live")
        self.log_dir = root / "logs" / ("mock" if mock else "live")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = ConfigStore(config_path)
        self.limiter = SlidingWindowLimiter.from_config(self.cfg.get("rate_limits"), self.cfg.get("rate_limiter"))
        if gateway is None:
            if mock:
                from .gateway.mock_gw import MockGateway
                gateway = MockGateway(self.cfg.get("mock"))
            else:
                from .gateway.moomoo_gw import MoomooGateway
                gateway = MoomooGateway(self.cfg.get("opend"))
        self.gw = gateway
        self.client = OpenDClient(self.gw, self.limiter, self.cfg)
        self.hub = Hub()
        self.store = MarketStore(self.hub)
        self.subs = SubscriptionManager(self.client, self.cfg, self.hub)
        self.stocks = StockService(self)
        self.options = OptionService(self, self.data_dir / "cache")
        self.trading = TradingService(self, self.data_dir, self.log_dir)
        self.alerts = AlertService(self, self.data_dir)
        self.status = StatusService(self)
        self.market = MarketService(self, self.data_dir / "cache")
        self.news = NewsService(self, self.data_dir)
        self.analysis = StockAnalysisService(self, self.data_dir)
        self.stocks.extra_codes = lambda: self.options.watched_codes() | {
            c for c in self.alerts.watched_codes() if not is_option_code(c)}
        self.options.extra_codes = lambda: {
            c for c in (self.alerts.watched_codes() | self.trading.watched_codes()) if is_option_code(c)}
        self._initialized = False
        self._connect_lock: asyncio.Lock | None = None

    async def start(self) -> None:
        self._connect_lock = asyncio.Lock()
        await self.hub.start()
        await self.gw.start(self.on_event)
        for svc in (self.stocks, self.options, self.alerts, self.trading, self.status, self.market, self.news,
                    self.analysis):
            await svc.start()
        log.info("backend started (%s mode)", "mock" if self.mock else "OpenD")

    async def stop(self) -> None:
        log.info("shutting down: releasing subscriptions and closing OpenD contexts")
        for svc in (self.analysis, self.news, self.market, self.status, self.alerts, self.trading, self.options,
                    self.stocks):
            try:
                await svc.stop()
            except Exception:
                log.exception("stopping %s failed", svc.__class__.__name__)
        try:
            await asyncio.wait_for(self.subs.shutdown(), timeout=15)
        except Exception:
            log.exception("releasing subscriptions failed")
        await self.gw.stop()
        await self.hub.stop()
        log.info("shutdown complete")

    # ----------------------------------------------------------- gateway events
    def on_event(self, kind: str, payload: Any) -> None:
        try:
            if kind == "quote":
                for r in payload or []:
                    self.store.update(r.get("code"), from_quote_push(r))
            elif kind == "kline":
                self.store.add_candles(payload or [])
            elif kind == "ticker":
                self.store.add_ticks(payload or [])
            elif kind == "orderbook":
                self.store.set_book(payload or {})
            elif kind == "sysnotify":
                self.status.on_sysnotify(payload)
            elif kind == "order":
                self.trading.on_order_push(payload)
            elif kind == "deal":
                self.trading.on_deal_push(payload)
            elif kind == "quote_connected":
                asyncio.create_task(self._on_connected())
            elif kind == "quote_disconnected":
                log.warning("OpenD quote connection lost: %s", payload)
                self.hub.publish("toast", {"level": "error", "text": "OpenD 连接已断开，正在自动重连……"})
            elif kind == "trade_connected":
                asyncio.create_task(self.trading.refresh_accounts())
            elif kind == "trade_disconnected":
                log.warning("OpenD trade connection lost: %s", payload)
        except Exception:
            log.exception("handling gateway event %s failed", kind)

    async def _on_connected(self) -> None:
        assert self._connect_lock is not None
        async with self._connect_lock:
            for _ in range(50):
                if self.gw.connection_state() == "connected":
                    break
                await asyncio.sleep(0.2)
            if self.gw.connection_state() != "connected":
                return
            if not self._initialized:
                self._initialized = True
                log.info("connected to %s; restoring configured subscriptions", self.gw.name)
                await self.stocks.restore_subscriptions()
                await self.options.restore()
                self.hub.publish("toast", {"level": "info", "text": "已连接 OpenD" if not self.mock else "已启动模拟行情"})
            else:
                log.info("reconnected; verifying subscriptions")
                await self.subs.restore()
            await self.status.refresh_rights()

    # --------------------------------------------------------------- bootstrap
    def bootstrap(self) -> dict:
        return {
            "mode": "mock" if self.mock else "opend",
            "status": self.status.view(),
            "watchlist": self.stocks.watchlist,
            "subs": self.subs.state(),
            "quotes": self.store.data,
            "options": self.options.config_view(),
            "chains": self.options.all_meta(),
            "alerts": self.alerts.list(),
            "trading": self.trading.public_state(),
            "stock_subtypes": [{"key": k, "label": SUBTYPE_ZH[k]} for k in STOCK_SUBTYPES],
            "condition_types": {k: v["label"] for k, v in CONDITION_TYPES.items()},
            "config_path": str(self.cfg.path),
            "hold_sec": self.subs.hold_sec,
            "market": self.market.state(),
            "news": {"settings": self.news.settings(), "earnings": self.news.earnings,
                     "events": self.news.query(levels=["高", "中"], limit=150)},
            "analysis": self.analysis.settings(),
        }
