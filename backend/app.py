"""FastAPI application: REST API, WebSocket, static frontend.

Local-only hardening (the API can place orders):
* the server binds to 127.0.0.1 only (see __main__)
* the Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding)
* state-changing requests need the X-Dashboard header and, if present, a local Origin
  (blocks cross-site requests from other web pages)
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .app_state import AppState
from .errors import UserError
from .gateway.base import Gateway, NotConnectedError, OpenDError
from .timeutil import et_date_str

log = logging.getLogger(__name__)


def create_app(config_path: Path, mock: bool, base_dir: Path, port: int,
               gateway: Gateway | None = None, extra_hosts: set[str] | None = None,
               data_root: Path | None = None) -> FastAPI:
    state = AppState(config_path, mock, base_dir, gateway, data_root)
    frontend = base_dir / "frontend"
    tokens_css = base_dir / "design" / "apple_liquid_glass" / "tokens.css"
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"} | (extra_hosts or set())
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.start()
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.s = state

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "仅允许通过 127.0.0.1 本机访问"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("x-dashboard") != "1" or (origin and origin not in allowed_origins):
                return JSONResponse({"detail": "请求被拒绝：只接受来自本看板页面的操作"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(UserError)
    async def _user_error(_: Request, e: UserError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(NotConnectedError)
    async def _not_connected(_: Request, e: NotConnectedError):
        return JSONResponse({"detail": "OpenD 未连接：请确认 OpenD 已启动并登录，看板会自动重连"}, status_code=503)

    @app.exception_handler(OpenDError)
    async def _opend_error(_: Request, e: OpenDError):
        return JSONResponse({"detail": f"OpenD 返回错误：{e.msg}"}, status_code=502)

    # ------------------------------------------------------------- frontend
    @app.get("/")
    async def index():
        return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/static/tokens.css")
    async def tokens():
        return FileResponse(tokens_css, media_type="text/css")

    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if websocket.headers.get("host") not in allowed_hosts or (origin and origin not in allowed_origins):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await state.hub.serve(websocket)

    # ------------------------------------------------------------------ api
    @app.get("/api/state")
    async def get_state():
        return state.bootstrap()

    # stocks
    @app.post("/api/stocks")
    async def add_stock(body: dict = Body(...)):
        return await state.stocks.add(str(body.get("code", "")))

    @app.delete("/api/stocks/{code}")
    async def remove_stock(code: str):
        return await state.stocks.remove(code)

    @app.post("/api/stocks/{code}/subtypes")
    async def set_subtype(code: str, body: dict = Body(...)):
        return await state.stocks.set_subtype(code, str(body.get("subtype")), bool(body.get("enabled")))

    @app.get("/api/stocks/{code}/detail")
    async def stock_detail(code: str):
        return state.store.detail(code)

    # options
    @app.get("/api/options/expirations/{underlying}")
    async def expirations(underlying: str, refresh: bool = False):
        return {"underlying": underlying, "expirations": await state.options.get_expirations(underlying, refresh)}

    @app.post("/api/options/underlyings")
    async def add_underlying(body: dict = Body(...)):
        return await state.options.add_underlying(str(body.get("code", "")))

    @app.delete("/api/options/underlyings/{underlying}")
    async def remove_underlying(underlying: str):
        return await state.options.remove_underlying(underlying)

    @app.post("/api/options/expirations")
    async def select_expiration(body: dict = Body(...)):
        return await state.options.select_expiration(str(body.get("underlying", "")), str(body.get("date", "")))

    @app.delete("/api/options/expirations/{underlying}/{date}")
    async def deselect_expiration(underlying: str, date: str):
        return await state.options.deselect_expiration(underlying, date)

    @app.patch("/api/options/expirations/{underlying}/{date}")
    async def update_expiration(underlying: str, date: str, body: dict = Body(...)):
        return await state.options.update_expiration(underlying, date, body)

    @app.post("/api/options/chains/{underlying}/{date}/refresh")
    async def refresh_chain(underlying: str, date: str):
        return await state.options.refresh_chain(underlying, date)

    @app.post("/api/options/pinned")
    async def add_pin(body: dict = Body(...)):
        return await state.options.add_pin(str(body.get("code", "")))

    @app.delete("/api/options/pinned/{code}")
    async def remove_pin(code: str):
        return await state.options.remove_pin(code)

    # alerts
    @app.get("/api/alerts")
    async def list_alerts():
        return state.alerts.list()

    @app.post("/api/alerts")
    async def create_alert(body: dict = Body(...)):
        return state.alerts.create(body)

    @app.put("/api/alerts/{rule_id}")
    async def update_alert(rule_id: str, body: dict = Body(...)):
        return state.alerts.update(rule_id, body)

    @app.delete("/api/alerts/{rule_id}")
    async def delete_alert(rule_id: str):
        return state.alerts.delete(rule_id)

    @app.post("/api/alerts/{rule_id}/rearm")
    async def rearm_alert(rule_id: str):
        return state.alerts.rearm(rule_id)

    @app.post("/api/alerts/{rule_id}/enabled")
    async def enable_alert(rule_id: str, body: dict = Body(...)):
        return state.alerts.set_enabled(rule_id, bool(body.get("enabled")))

    # trading
    @app.get("/api/trading")
    async def trading_state():
        return state.trading.public_state()

    @app.post("/api/trading/auto")
    async def set_auto(body: dict = Body(...)):
        return await state.trading.set_auto(bool(body.get("enabled")))

    @app.post("/api/trading/env")
    async def set_env(body: dict = Body(...)):
        return state.trading.prepare_env(str(body.get("env", "")))

    @app.post("/api/trading/env/confirm")
    async def confirm_env(body: dict = Body(...)):
        return state.trading.confirm_env(str(body.get("challenge", "")), str(body.get("phrase", "")))

    @app.post("/api/trading/real_account")
    async def set_real_account(body: dict = Body(...)):
        return state.trading.set_real_account(str(body.get("acc_id", "")))

    @app.put("/api/trading/limits")
    async def set_limits(body: dict = Body(...)):
        return state.trading.set_limits(dict(body.get("limits") or {}), bool(body.get("confirmed")))

    @app.post("/api/trading/emergency_stop")
    async def emergency_stop():
        return await state.trading.emergency_stop()

    @app.post("/api/trading/combo_check")
    async def combo_check(body: dict = Body(...)):
        return await state.trading.check_combo(list(body.get("legs") or []))

    @app.get("/api/trading/log")
    async def order_log(limit: int = 300):
        path = state.trading.olog.dir / f"orders-{et_date_str()}.jsonl"
        if not path.exists():
            return {"path": str(path), "entries": []}
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 2000)):]
        return {"path": str(path), "entries": [json.loads(x) for x in lines if x.strip()]}

    # market overview
    @app.get("/api/market")
    async def market_state():
        return state.market.state()

    @app.post("/api/market/plate_set")
    async def market_plate_set(body: dict = Body(...)):
        return state.market.set_plate_set(str(body.get("plate_set", "")))

    @app.put("/api/market/size_filter")
    async def market_size_filter(body: dict = Body(...)):
        return state.market.set_size_filter(body.get("min_market_cap_usd"), body.get("min_dollar_volume_usd"))

    @app.put("/api/market/benchmarks")
    async def market_benchmarks(body: dict = Body(...)):
        return await state.market.set_benchmarks(list(body.get("codes") or []))

    @app.post("/api/market/refresh")
    async def market_refresh():
        return state.market.request_refresh()

    @app.get("/api/market/sector/{code}")
    async def market_sector(code: str):
        return await state.market.sector_detail(code)

    # news
    @app.get("/api/news")
    async def news_list(symbol: str = "", levels: str = "", source: str = "", limit: int = 150, hours: float = 72):
        lv = [x for x in levels.split(",") if x in ("高", "中", "低")]
        return {"events": state.news.query(symbol or None, lv or None, source or None, min(max(limit, 1), 500), hours),
                "earnings": state.news.earnings}

    @app.get("/api/news/settings")
    async def news_settings():
        return state.news.settings()

    @app.put("/api/news/settings")
    async def news_settings_update(body: dict = Body(...)):
        return state.news.update_settings(body)

    # 个股分析 (the Anthropic key stays in the server environment; nothing secret is returned)
    # Off unless stock_analysis.enabled is true: every route except settings answers 404.
    def analysis_off() -> JSONResponse | None:
        if state.analysis.enabled():
            return None
        return JSONResponse({"detail": "个股分析已关闭（config.yaml 中 stock_analysis.enabled: false）"}, status_code=404)

    @app.post("/api/stock-analysis")
    async def stock_analysis(body: dict = Body(...)):
        if off := analysis_off():
            return off
        r = await state.analysis.analyze(str(body.get("ticker", "")), bool(body.get("refresh")))
        return JSONResponse(r, status_code=202 if r.get("status") == "running" else 200)

    @app.get("/api/stock-analysis/job/{ticker}")
    async def stock_analysis_job(ticker: str):
        return analysis_off() or state.analysis.job(ticker)

    @app.get("/api/stock-analysis/search")
    async def stock_analysis_search(q: str = ""):
        return analysis_off() or {"candidates": await state.analysis.search(q[:40])}

    @app.get("/api/stock-analysis/settings")
    async def stock_analysis_settings():
        return state.analysis.settings()

    # status
    @app.post("/api/status/quote_right")
    async def quote_right():
        return await state.status.request_quote_right()

    # mock-only helpers
    @app.post("/api/mock/{action}")
    async def mock_action(action: str, body: dict = Body(default={})):
        if not state.gw.is_mock:
            raise UserError("仅模拟模式可用")
        try:
            return {"message": await state.gw.mock_action(action, **(body or {}))}
        except (TypeError, KeyError, ValueError) as e:
            raise UserError(f"参数错误：{e}") from None

    return app
