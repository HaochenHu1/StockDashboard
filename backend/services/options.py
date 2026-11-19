"""Options: expirations, daily chain cache, quote modes per expiration, polling, pinned contracts.

The chain endpoint has the tightest rate limit (10 / 30 s) and returns static
contract data only, so each chain is fetched once per US trading day (at startup,
on first request, or by the manual refresh button) and cached on disk.
Quotes come either from subscription (QUOTE + ORDER_BOOK pushes; 1 option quota
per chain per subtype) or from snapshot polling (no quota; batches of up to 400
codes; background priority so orders are never delayed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..alerts import normalize_code
from ..gateway.base import OpenDError, is_option_code, parse_option_code
from ..ratelimit import Priority
from ..timeutil import et_date_str, et_str, now_et
from .market import from_snapshot
from .stocks import validate_stock_code
from .subscriptions import UserError

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)


def chain_key(underlying: str, expiry: str) -> str:
    return f"CHAIN|{underlying}|{expiry}"


class OptionService:
    def __init__(self, app: "AppState", cache_dir: Path):
        self.app = app
        self.cache_dir = cache_dir
        self.chains: dict[tuple[str, str], dict] = {}
        self.expirations: dict[str, dict] = {}
        self.extra_codes: Callable[[], set[str]] = lambda: set()
        self._next_due: dict[Any, float] = {}
        self._inflight: set[Any] = set()
        self._task: asyncio.Task | None = None
        self._chain_locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------- config
    @property
    def underlyings(self) -> list[dict]:
        return self.app.cfg.get("options", "underlyings", default=[]) or []

    @property
    def pinned(self) -> list[str]:
        return self.app.cfg.get("options", "pinned", default=[]) or []

    def _opt(self, key: str, default: Any) -> Any:
        return self.app.cfg.get("options", key, default=default)

    def _save_underlyings(self, items: list[dict]) -> None:
        self.app.cfg.set(("options", "underlyings"), items)
        self.app.hub.publish("options_config", self.config_view())

    def _find(self, items: list[dict], u: str, d: str | None = None) -> tuple[dict | None, dict | None]:
        ui = next((x for x in items if x["code"] == u), None)
        if ui is None or d is None:
            return ui, None
        return ui, next((e for e in ui.get("expirations", []) if e["date"] == d), None)

    def config_view(self) -> dict:
        return {"underlyings": self.underlyings, "pinned": self.pinned,
                "default_interval_sec": self._opt("default_interval_sec", 5),
                "min_interval_sec": self._opt("min_interval_sec", 1),
                "subscription_subtypes": self._opt("subscription_subtypes", ["QUOTE", "ORDER_BOOK"])}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        today = et_date_str()
        items = self.underlyings
        changed = False
        for u in items:
            keep = [e for e in u.get("expirations", []) if e["date"] >= today]
            if len(keep) != len(u.get("expirations", [])):
                log.info("dropping expired expirations for %s", u["code"])
                u["expirations"] = keep
                changed = True
        if changed:
            self._save_underlyings(items)
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def restore(self) -> None:
        """Startup: load chains (cache or API) and re-subscribe chains in subscription mode."""
        for u in self.underlyings:
            for e in u.get("expirations", []):
                try:
                    await self.ensure_chain(u["code"], e["date"])
                    if e.get("mode") == "subscribe":
                        await self._subscribe_chain(u["code"], e["date"])
                except (UserError, OpenDError) as err:
                    log.warning("restore chain %s %s failed: %s", u["code"], e["date"], err)
                    self.app.hub.publish("toast", {"level": "error",
                                                   "text": f"{u['code']} {e['date']}：{getattr(err, 'msg', err)}"})

    # ---------------------------------------------------------------- cache
    def _chain_path(self, u: str, d: str) -> Path:
        return self.cache_dir / "chains" / et_date_str() / f"{u.replace('.', '_')}_{d}.json"

    def _exp_path(self, u: str) -> Path:
        return self.cache_dir / "expirations" / et_date_str() / f"{u.replace('.', '_')}.json"

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    async def get_expirations(self, u: str, refresh: bool = False) -> list[dict]:
        u = normalize_code(u)
        today = et_date_str()
        cached = self.expirations.get(u)
        if cached and cached["date"] == today and not refresh:
            return cached["list"]
        path = self._exp_path(u)
        if path.exists() and not refresh:
            lst = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                recs = await self.app.client.option_expirations(u, Priority.NORMAL)
            except OpenDError as e:
                raise UserError(f"获取 {u} 期权到期日失败：{e.msg}") from None
            lst = sorted(({"date": r["strike_time"], "dte": r.get("option_expiry_date_distance"),
                           "cycle": r.get("expiration_cycle")} for r in recs if r.get("strike_time")),
                         key=lambda x: x["date"])
            lst = [x for x in lst if x["date"] >= today]
            self._write(path, lst)
        self.expirations[u] = {"date": today, "list": lst}
        return lst

    async def ensure_chain(self, u: str, d: str, refresh: bool = False) -> dict:
        lock = self._chain_locks.setdefault((u, d), asyncio.Lock())
        async with lock:
            today = et_date_str()
            ch = self.chains.get((u, d))
            if ch and ch["day"] == today and not refresh:
                return ch
            path = self._chain_path(u, d)
            if path.exists() and not refresh:
                ch = json.loads(path.read_text(encoding="utf-8"))
            else:
                try:
                    recs = await self.app.client.option_chain(u, d, d, Priority.NORMAL)
                except OpenDError as e:
                    raise UserError(f"获取 {u} {d} 期权链失败：{e.msg}") from None
                contracts = [{"code": r["code"], "option_type": r.get("option_type"),
                              "strike": float(r["strike_price"]), "lot_size": r.get("lot_size"),
                              "name": r.get("name")}
                             for r in recs if r.get("strike_time") == d and r.get("strike_price") is not None]
                if not contracts:
                    raise UserError(f"{u} {d} 没有返回期权合约")
                by_strike: dict[float, dict] = {}
                for c in contracts:
                    row = by_strike.setdefault(c["strike"], {"strike": c["strike"], "call": None, "put": None})
                    row["call" if c["option_type"] == "CALL" else "put"] = c["code"]
                ch = {"underlying": u, "expiry": d, "day": today, "fetched_at": et_str(),
                      "contracts": contracts, "strikes": [by_strike[k] for k in sorted(by_strike)]}
                self._write(path, ch)
                log.info("fetched chain %s %s: %d contracts", u, d, len(contracts))
            old = self.chains.get((u, d))
            self.chains[(u, d)] = ch
            if refresh and old:
                new_codes = [c["code"] for c in ch["contracts"]]
                try:
                    await self.app.subs.add_codes(chain_key(u, d), new_codes)
                except OpenDError as e:
                    log.warning("subscribe new contracts failed: %s", e)
            return ch

    # ----------------------------------------------------------- user actions
    async def add_underlying(self, raw: str) -> dict:
        u = validate_stock_code(raw)
        items = self.underlyings
        if any(x["code"] == u for x in items):
            raise UserError(f"{u} 已在期权标的列表中")
        exps = await self.get_expirations(u, refresh=True)
        if not exps:
            raise UserError(f"{u} 没有可用的期权到期日")
        items.append({"code": u, "expirations": []})
        self._save_underlyings(items)
        if u not in self.app.stocks.codes():
            try:
                for rec in await self.app.client.snapshot([u], Priority.NORMAL):
                    self.app.store.update(rec["code"], from_snapshot(rec))
            except OpenDError:
                pass
        return {"code": u, "expirations": exps}

    async def remove_underlying(self, u: str) -> dict:
        u = normalize_code(u)
        items = self.underlyings
        ui, _ = self._find(items, u)
        if ui is None:
            raise UserError(f"{u} 不在期权标的列表中")
        msgs = []
        for e in ui.get("expirations", []):
            if e.get("mode") == "subscribe":
                msgs += await self.app.subs.unsubscribe(chain_key(u, e["date"]), self._subtypes())
        self._save_underlyings([x for x in items if x["code"] != u])
        return {"messages": msgs}

    async def select_expiration(self, u: str, d: str) -> dict:
        u = normalize_code(u)
        items = self.underlyings
        ui, e = self._find(items, u, d)
        if ui is None:
            raise UserError(f"请先添加期权标的 {u}")
        if e is not None:
            return self.chain_meta(u, d)
        if d not in [x["date"] for x in await self.get_expirations(u)]:
            raise UserError(f"{d} 不是 {u} 的有效到期日")
        await self.ensure_chain(u, d)
        ui.setdefault("expirations", []).append({
            "date": d, "mode": "snapshot", "interval_sec": float(self._opt("default_interval_sec", 5)),
            "strikes_each_side": int(self._opt("default_strikes_each_side", 8))})
        ui["expirations"].sort(key=lambda x: x["date"])
        self._save_underlyings(items)
        return self.chain_meta(u, d)

    async def deselect_expiration(self, u: str, d: str) -> dict:
        u = normalize_code(u)
        items = self.underlyings
        ui, e = self._find(items, u, d)
        if e is None:
            raise UserError(f"{u} {d} 未被选中")
        msgs = []
        if e.get("mode") == "subscribe":
            msgs = await self.app.subs.unsubscribe(chain_key(u, d), self._subtypes())
        ui["expirations"] = [x for x in ui["expirations"] if x["date"] != d]
        self._save_underlyings(items)
        return {"messages": msgs}

    def _subtypes(self) -> list[str]:
        return list(self._opt("subscription_subtypes", ["QUOTE", "ORDER_BOOK"]))

    async def _subscribe_chain(self, u: str, d: str) -> list[str]:
        ch = await self.ensure_chain(u, d)
        codes = [c["code"] for c in ch["contracts"]]
        return await self.app.subs.subscribe(chain_key(u, d), codes, self._subtypes(), True)

    async def update_expiration(self, u: str, d: str, body: dict) -> dict:
        u = normalize_code(u)
        items = self.underlyings
        ui, e = self._find(items, u, d)
        if e is None:
            raise UserError(f"{u} {d} 未被选中")
        msgs: list[str] = []
        if "interval_sec" in body:
            try:
                iv = float(body["interval_sec"])
            except (TypeError, ValueError):
                raise UserError("轮询间隔必须是数字") from None
            min_iv = float(self._opt("min_interval_sec", 1))
            if iv < min_iv:
                raise UserError(f"轮询间隔不能小于 {min_iv:g} 秒")
            e["interval_sec"] = iv
        if "strikes_each_side" in body:
            try:
                n = int(body["strikes_each_side"])
            except (TypeError, ValueError):
                raise UserError("档数必须是整数") from None
            if not 1 <= n <= 100:
                raise UserError("上下档数应在 1 到 100 之间")
            e["strikes_each_side"] = n
        if "mode" in body and body["mode"] != e.get("mode"):
            mode = body["mode"]
            if mode not in ("snapshot", "subscribe"):
                raise UserError("报价模式只能是“快照轮询”或“订阅”")
            if mode == "subscribe":
                msgs += await self._subscribe_chain(u, d)
                n = len(self._subtypes())
                msgs.append(f"已切换为订阅模式（占用 {n} 个期权订阅额度：每种数据类型每条期权链 1 个）")
            else:
                msgs += await self.app.subs.unsubscribe(chain_key(u, d), self._subtypes())
                msgs.append("已切换为快照轮询（不占用订阅额度）")
            e["mode"] = mode
        self._save_underlyings(items)
        return {"meta": self.chain_meta(u, d), "messages": msgs}

    async def refresh_chain(self, u: str, d: str) -> dict:
        u = normalize_code(u)
        await self.ensure_chain(u, d, refresh=True)
        meta = self.chain_meta(u, d)
        self.app.hub.publish("chain", meta)
        return meta

    async def add_pin(self, raw: str) -> dict:
        code = normalize_code(raw)
        if not is_option_code(code):
            raise UserError(f"{code} 不是期权合约代码（格式如 US.QQQ261113C675000）")
        pins = self.pinned
        if code in pins:
            raise UserError("该合约已在期权自选中")
        try:
            recs = await self.app.client.snapshot([code], Priority.NORMAL)
        except OpenDError as e:
            raise UserError(f"无法获取 {code} 的报价：{e.msg}") from None
        for rec in recs:
            self.app.store.update(rec["code"], from_snapshot(rec))
        pins.append(code)
        self.app.cfg.set(("options", "pinned"), pins)
        self.app.hub.publish("options_config", self.config_view())
        return {"code": code}

    async def remove_pin(self, raw: str) -> dict:
        code = normalize_code(raw)
        pins = [p for p in self.pinned if p != code]
        self.app.cfg.set(("options", "pinned"), pins)
        self.app.hub.publish("options_config", self.config_view())
        return {"code": code}

    # ----------------------------------------------------------------- views
    def chain_meta(self, u: str, d: str) -> dict:
        ch = self.chains.get((u, d))
        _, e = self._find(self.underlyings, u, d)
        e = e or {}
        dte = None
        try:
            import datetime as _dt
            dte = (_dt.date.fromisoformat(d) - now_et().date()).days
        except ValueError:
            pass
        return {"underlying": u, "expiry": d, "dte": dte,
                "fetched_at": ch["fetched_at"] if ch else None,
                "strikes": ch["strikes"] if ch else [],
                "mode": e.get("mode", "snapshot"), "interval_sec": e.get("interval_sec"),
                "strikes_each_side": e.get("strikes_each_side"),
                "subscribed": self.app.subs.state().get(chain_key(u, d), {})}

    def all_meta(self) -> list[dict]:
        return [self.chain_meta(u["code"], e["date"]) for u in self.underlyings for e in u.get("expirations", [])]

    def window_codes(self, u: str, d: str, n: int) -> list[str]:
        ch = self.chains.get((u, d))
        if not ch or not ch["strikes"]:
            return []
        strikes = ch["strikes"]
        q = self.app.store.get(u) or {}
        px = q.get("eff_price") or q.get("last")
        if px:
            idx = min(range(len(strikes)), key=lambda i: abs(strikes[i]["strike"] - px))
        else:
            idx = len(strikes) // 2
        rows = strikes[max(0, idx - n): idx + n + 1]
        return [c for r in rows for c in (r["call"], r["put"]) if c]

    def watched_codes(self) -> set[str]:
        """Underlyings whose price is needed for the ATM strike."""
        return {u["code"] for u in self.underlyings if u.get("expirations")} | {
            parse_option_code(p)["underlying"] for p in self.pinned if parse_option_code(p)}

    # ------------------------------------------------------------------ poll
    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self.app.gw.connection_state() != "connected":
                continue
            now = time.monotonic()
            min_iv = float(self._opt("min_interval_sec", 1))
            for u in self.underlyings:
                for e in u.get("expirations", []):
                    if e.get("mode") == "subscribe":
                        continue
                    key = (u["code"], e["date"])
                    if key in self._inflight or now < self._next_due.get(key, 0):
                        continue
                    self._next_due[key] = now + max(min_iv, float(e.get("interval_sec") or 5))
                    codes = self.window_codes(u["code"], e["date"], int(e.get("strikes_each_side") or 8))
                    if codes:
                        self._dispatch(key, codes)
            key = "PINNED"
            if key not in self._inflight and now >= self._next_due.get(key, 0):
                self._next_due[key] = now + max(min_iv, float(self._opt("pinned_refresh_sec", 5)))
                codes = sorted(set(self.pinned) | {c for c in self.extra_codes() if is_option_code(c)})
                if codes:
                    self._dispatch(key, codes)

    def _dispatch(self, key: Any, codes: list[str]) -> None:
        self._inflight.add(key)
        asyncio.create_task(self._poll(key, codes))

    async def _poll(self, key: Any, codes: list[str]) -> None:
        try:
            for rec in await self.app.client.snapshot(codes, Priority.POLL):
                self.app.store.update(rec["code"], from_snapshot(rec))
        except OpenDError as e:
            log.debug("option poll %s failed: %s", key, e)
        except Exception:
            log.exception("option poll %s crashed", key)
        finally:
            self._inflight.discard(key)
