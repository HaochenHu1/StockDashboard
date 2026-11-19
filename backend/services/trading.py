"""Automatic ordering - OFF by default, behind a master switch.

Safety rules implemented here
* The master switch always starts OFF; it is never persisted as ON.
* The environment always starts as paper trading (SIMULATE). Switching to live
  trading needs a two-step confirmation (challenge token + typed phrase) and turns
  the master switch off; trading must be unlocked by hand in the OpenD GUI. The code
  never calls unlock_trade and never touches a password.
* Limit orders only. Default limit = mid price at the moment of submission (fresh
  snapshot / strategy analysis), plus a configurable offset.
* Hard limits from the config file: max contracts per order, max automatic entry
  orders per day, max daily loss (turns the switch off), stop loss, take profit
  target and time stop.
* Emergency stop: switch off at once and cancel every open order. Paper accounts do
  not support cancel_all_order, so open orders are also cancelled one by one.
* Every order, modification, cancellation and fill report goes to
  logs/orders/orders-YYYY-MM-DD.jsonl with timestamp, contract, side, quantity,
  price, triggering rule and the raw API response.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import secrets
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..alerts import Rule, normalize_code
from ..errors import UserError
from ..gateway.base import OpenDError, is_option_code, parse_option_code
from ..ratelimit import Priority
from ..timeutil import et_date_str, et_str, now_et, parse_hhmm

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)

OPEN_STATUSES = {"UNSUBMITTED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}
FINAL_STATUSES = {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED",
                  "SUBMIT_FAILED", "TIMEOUT", "FILL_CANCELLED"}
ACTIVE = ("entry_pending", "open", "exit_pending")
REAL_PHRASE = "确认实盘交易"
ENV_ZH = {"SIMULATE": "模拟交易", "REAL": "实盘交易"}
LIMIT_SCHEMA: dict[str, tuple[type, float, float, str]] = {
    "max_contracts_per_order": (int, 1, 1000, "单笔最大合约数"),
    "max_orders_per_day": (int, 1, 1000, "每日最大自动开仓次数"),
    "max_daily_loss_usd": (float, 1, 10_000_000, "每日最大亏损（美元）"),
    "stop_loss_pct": (float, 1, 100, "止损百分比"),
    "take_profit_pct": (float, 1, 10_000, "止盈目标百分比"),
    "mid_offset": (float, -100, 100, "相对中间价的偏移"),
    "entry_timeout_sec": (int, 5, 3600, "入场单超时（秒）"),
    "exit_reprice_sec": (int, 5, 3600, "平仓单重新报价间隔（秒）"),
}


def mask(acc_id: Any) -> str:
    s = str(acc_id or "")
    return f"****{s[-4:]}" if s else "—"


def round_price(p: float) -> float:
    return max(0.01, round(p + 1e-9, 2))


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class OrderLog:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.recent: deque[dict] = deque(maxlen=300)

    def write(self, event: str, **fields: Any) -> dict:
        rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "ts_et": et_str(),
               "event": event, **fields}
        path = self.dir / f"orders-{et_date_str()}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.recent.append(rec)
        return rec


class TradingService:
    def __init__(self, app: "AppState", data_dir: Path, log_dir: Path):
        self.app = app
        self.state_path = data_dir / "trading_state.json"
        self.olog = OrderLog(log_dir / "orders")
        self.auto_enabled = False
        self.env = "SIMULATE"
        self.accounts: list[dict] = []
        self.sim_acc_id: int | None = None
        self.combo: dict[str, Any] = {"status": "unknown", "message": "尚未检测", "paper_verified": False}
        self.daily: dict[str, Any] = {"date": et_date_str(), "entries": 0, "realized": 0.0, "loss_locked": False}
        self.positions: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.challenge: dict | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_poll = 0.0
        self._load()

    # --------------------------------------------------------------- state
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("trading state unreadable; starting fresh")
            return
        if raw.get("daily", {}).get("date") == et_date_str():
            self.daily.update(raw["daily"])
        self.combo.update(raw.get("combo", {}))
        for p in raw.get("positions", []):
            if p.get("status") in ACTIVE:
                p["note"] = "程序重启后恢复：自动下单开启后继续管理"
            self.positions[p["id"]] = p

    def _save(self) -> None:
        keep = sorted(self.positions.values(), key=lambda p: p["created_ts"])[-200:]
        data = {"daily": self.daily, "combo": self.combo, "positions": keep}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def _rollover(self) -> None:
        today = et_date_str()
        if self.daily["date"] != today:
            self.daily = {"date": today, "entries": 0, "realized": 0.0, "loss_locked": False}
            self._save()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self._save()

    # ------------------------------------------------------------ accounts
    @property
    def real_acc_id(self) -> int | None:
        v = self.app.cfg.get("auto_trading", "real_acc_id")
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    async def refresh_accounts(self) -> None:
        try:
            accs = await self.app.client.acc_list()
        except OpenDError as e:
            log.warning("get_acc_list failed: %s", e)
            return
        self.accounts = accs

        def us(a: dict) -> bool:
            auth = a.get("trdmarket_auth") or []
            return "US" in auth if isinstance(auth, list) else "US" in str(auth)
        sims = [a for a in accs if a.get("trd_env") == "SIMULATE" and us(a)]
        sims.sort(key=lambda a: 0 if a.get("sim_acc_type") in ("STOCK_AND_OPTION", "OPTION") else 1)
        self.sim_acc_id = int(sims[0]["acc_id"]) if sims else None
        log.info("accounts loaded: paper=%s, real=%d", mask(self.sim_acc_id),
                 sum(1 for a in accs if a.get("trd_env") == "REAL"))
        self.publish()

    def real_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            if a.get("trd_env") != "REAL":
                continue
            auth = a.get("trdmarket_auth") or []
            if "US" not in (auth if isinstance(auth, list) else str(auth)):
                continue
            out.append({"acc_id": str(a["acc_id"]), "label": f"{mask(a['acc_id'])} · {a.get('acc_type') or ''}"})
        return out

    def acc_for(self, env: str) -> int | None:
        if env == "SIMULATE":
            return self.sim_acc_id
        real = self.real_acc_id
        if real and any(str(a["acc_id"]) == str(real) for a in self.accounts if a.get("trd_env") == "REAL"):
            return real
        return None

    def limits(self) -> dict:
        return self.app.cfg.get("auto_trading", "limits", default={}) or {}

    # -------------------------------------------------------------- views
    def _unrealized(self, p: dict) -> float | None:
        if p["status"] not in ("open", "exit_pending") or not p.get("entry_price"):
            return None
        mark = self._mark(p)
        if mark is None:
            return None
        sign = 1 if p["side"] == "BUY" else -1
        return round((mark - p["entry_price"]) * p["filled_qty"] * 100 * sign, 2)

    def daily_view(self) -> dict:
        unreal = sum(u for p in self.positions.values() if (u := self._unrealized(p)) is not None)
        L = self.limits()
        return {"date": self.daily["date"], "entries": self.daily["entries"],
                "max_orders": L.get("max_orders_per_day"), "realized": round(self.daily["realized"], 2),
                "unrealized": round(unreal, 2), "total": round(self.daily["realized"] + unreal, 2),
                "max_loss": L.get("max_daily_loss_usd"), "loss_locked": self.daily.get("loss_locked", False)}

    def public_state(self) -> dict:
        today_orders = sorted(self.orders.values(), key=lambda o: str(o.get("create_time", "")), reverse=True)
        positions = sorted(self.positions.values(), key=lambda p: p["created_ts"], reverse=True)[:40]
        for p in positions:
            p["unrealized"] = self._unrealized(p)
        return {
            "auto_enabled": self.auto_enabled, "env": self.env, "trade_ready": self.app.gw.trade_ready(),
            "sim_account": mask(self.sim_acc_id) if self.sim_acc_id else None,
            "real_account": mask(self.real_acc_id) if self.real_acc_id else None,
            "real_account_id": str(self.real_acc_id) if self.real_acc_id else None,
            "real_accounts": self.real_accounts(), "limits": self.limits(), "daily": self.daily_view(),
            "positions": positions, "orders": [self._order_view(o) for o in today_orders[:60]],
            "combo": self.combo, "log": list(self.olog.recent)[-120:], "phrase": REAL_PHRASE,
        }

    @staticmethod
    def _order_view(o: dict) -> dict:
        return {k: o.get(k) for k in ("order_id", "code", "trd_side", "qty", "price", "order_status", "dealt_qty",
                                      "dealt_avg_price", "create_time", "updated_time", "remark", "trd_env",
                                      "last_err_msg")}

    def publish(self) -> None:
        self.app.hub.publish("trading", self.public_state())

    def toast(self, level: str, text: str) -> None:
        self.app.hub.publish("toast", {"level": level, "text": text})

    # ------------------------------------------------------------ switches
    async def set_auto(self, enabled: bool) -> dict:
        self._rollover()
        if enabled:
            if not self.app.gw.trade_ready():
                raise UserError("交易连接未就绪，无法开启自动下单")
            if self.acc_for(self.env) is None:
                raise UserError(f"没有可用的{ENV_ZH[self.env]}账户，无法开启自动下单")
            if self.daily.get("loss_locked"):
                raise UserError("今日已达到最大亏损限制，自动下单已锁定到下一个美东交易日")
        self.auto_enabled = bool(enabled)
        self.olog.write("auto_switch", enabled=self.auto_enabled, env=self.env)
        self.publish()
        return {"auto_enabled": self.auto_enabled}

    def prepare_env(self, env: str) -> dict:
        if env == "SIMULATE":
            if self.env != "SIMULATE":
                self.env = "SIMULATE"
                self.auto_enabled = False
                self.olog.write("env_switch", env="SIMULATE", note="自动下单已关闭")
                self.publish()
            return {"done": True, "env": self.env}
        if env != "REAL":
            raise UserError("未知交易环境")
        if self.env == "REAL":
            return {"done": True, "env": "REAL"}
        real = self.acc_for("REAL")
        if real is None:
            raise UserError("请先选择实盘账户")
        self.challenge = {"token": secrets.token_urlsafe(12), "expires": time.time() + 120}
        return {"done": False, "challenge": self.challenge["token"], "account": mask(real), "phrase": REAL_PHRASE}

    def confirm_env(self, token: str, phrase: str) -> dict:
        ch = self.challenge
        if not ch or ch["token"] != token or time.time() > ch["expires"]:
            self.challenge = None
            raise UserError("确认已过期，请重新切换")
        if (phrase or "").strip() != REAL_PHRASE:
            raise UserError(f"请输入“{REAL_PHRASE}”以完成第二步确认")
        self.challenge = None
        self.env = "REAL"
        self.auto_enabled = False
        self.olog.write("env_switch", env="REAL", account=mask(self.acc_for("REAL")), note="自动下单已关闭，需要重新开启")
        self.toast("warn", "已切换到实盘交易。请在 OpenD 界面手动解锁交易；自动下单已关闭，需要时请重新开启。")
        self.publish()
        return {"done": True, "env": "REAL"}

    def set_real_account(self, acc_id: str) -> dict:
        if self.env == "REAL":
            raise UserError("请先切回模拟交易，再更换实盘账户")
        if not any(a["acc_id"] == str(acc_id) for a in self.real_accounts()):
            raise UserError("所选账户不是可交易美股的实盘账户")
        self.app.cfg.set(("auto_trading", "real_acc_id"), int(acc_id))
        self.publish()
        return {"real_account": mask(acc_id)}

    def set_limits(self, new: dict, confirmed: bool) -> dict:
        if not confirmed:
            raise UserError("修改硬性限制需要确认")
        cur = self.limits()
        merged = dict(cur)
        for key, (typ, lo, hi, label) in LIMIT_SCHEMA.items():
            if key not in new:
                continue
            try:
                val = typ(new[key])
            except (TypeError, ValueError):
                raise UserError(f"{label}必须是数字") from None
            if not lo <= val <= hi:
                raise UserError(f"{label}应在 {lo:g} 到 {hi:g} 之间")
            merged[key] = val
        if "time_stop_et" in new:
            try:
                merged["time_stop_et"] = parse_hhmm(new["time_stop_et"]).strftime("%H:%M")
            except (ValueError, TypeError):
                raise UserError("时间止损格式应为 HH:MM（美东时间）") from None
        self.app.cfg.set(("auto_trading", "limits"), merged)
        self.olog.write("limits_changed", old=cur, new=merged)
        self.publish()
        return {"limits": merged}

    # ------------------------------------------------------ action checks
    def validate_action(self, act: dict | None, require_switch: bool = True) -> dict | None:
        """Normalize a rule's order_action. Enabled actions need the master switch ON."""
        if not act:
            return None
        enabled = bool(act.get("enabled"))
        kind = act.get("type") or "single"
        out: dict[str, Any] = {"enabled": enabled, "type": kind, "limit_price": "mid"}
        if (act.get("limit_price") or "mid") != "mid":
            raise UserError("只支持限价单，限价默认取提交时的中间价")
        L = self.limits()
        try:
            qty = int(act.get("quantity") or 1)
        except (TypeError, ValueError):
            raise UserError("下单数量必须是整数") from None
        out["quantity"] = qty
        if kind == "single":
            code = normalize_code(act.get("contract")) if act.get("contract") else None
            side = str(act.get("side") or "BUY").upper()
            out.update({"contract": code, "side": side})
            if not enabled:
                return out
            if not code or not is_option_code(code):
                raise UserError("请选择要下单的期权合约")
            if side not in ("BUY", "SELL"):
                raise UserError("方向只能是买入或卖出")
        elif kind == "combo":
            if not enabled:
                out["legs"] = [{"code": normalize_code(l.get("code")), "side": str(l.get("side") or "BUY").upper(),
                                "ratio": l.get("ratio") or 1} for l in (act.get("legs") or [])]
                return out
            if self.combo.get("status") == "unsupported":
                raise UserError("当前账户不支持组合单（已在模拟盘检测），无法使用组合下单")
            out["legs"] = self.validate_legs(act.get("legs") or [])
        else:
            raise UserError("未知下单类型")
        if enabled:
            if require_switch and not self.auto_enabled:
                raise UserError("自动下单总开关关闭时，不能为规则关联下单动作。请先在“自动交易”页面打开总开关")
            max_c = int(L.get("max_contracts_per_order", 1))
            if not 1 <= qty <= max_c:
                raise UserError(f"下单数量应在 1 到 {max_c} 之间（单笔最大合约数，硬性限制）")
        return out

    @staticmethod
    def validate_legs(legs_raw: list[dict]) -> list[dict]:
        if not 2 <= len(legs_raw) <= 4:
            raise UserError("组合单需要 2 到 4 条腿")
        legs, unders = [], set()
        for i, leg in enumerate(legs_raw, 1):
            code = normalize_code(leg.get("code"))
            side = str(leg.get("side") or "BUY").upper()
            try:
                ratio = int(leg.get("ratio") or 1)
            except (TypeError, ValueError):
                raise UserError(f"第 {i} 条腿的比例必须是整数") from None
            info = parse_option_code(code)
            if not info:
                raise UserError(f"第 {i} 条腿不是期权合约代码")
            if side not in ("BUY", "SELL") or not 1 <= ratio <= 10:
                raise UserError(f"第 {i} 条腿的方向或比例无效")
            unders.add(info["underlying"])
            legs.append({"code": code, "side": side, "ratio": ratio})
        if len(unders) != 1:
            raise UserError("组合单的所有腿必须是同一标的")
        return legs

    # ------------------------------------------------------------- prices
    async def _fresh_mid(self, code: str) -> tuple[float, float, float]:
        recs = await self.app.client.snapshot([code], Priority.ORDER)
        if not recs:
            raise UserError(f"无法获取 {code} 的报价")
        bid, ask = _f(recs[0].get("bid_price")), _f(recs[0].get("ask_price"))
        if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
            raise UserError(f"{code} 当前没有有效的买卖报价（买 {bid}，卖 {ask}），未下单")
        return bid, ask, (bid + ask) / 2

    async def _combo_mid(self, legs: list[dict]) -> tuple[float, float, float]:
        an = await self.app.client.strategy_analysis(legs, Priority.ORDER)
        bid1, ask1 = _f(an.get("bid1")), _f(an.get("ask1"))
        if bid1 is None or ask1 is None:
            raise UserError("无法获取组合的买卖价（get_option_strategy_analysis 未返回 bid1/ask1）")
        return bid1, ask1, (bid1 + ask1) / 2

    def _mark(self, p: dict) -> float | None:
        if p["kind"] == "single":
            q = self.app.store.get(p["code"]) or {}
            return q.get("mid")
        total = 0.0
        for leg in p["legs"]:
            q = self.app.store.get(leg["code"]) or {}
            if q.get("mid") is None:
                return None
            total += (1 if leg["side"] == "BUY" else -1) * leg["ratio"] * q["mid"]
        return total

    def watched_codes(self) -> set[str]:
        out: set[str] = set()
        for p in self.positions.values():
            if p["status"] in ACTIVE:
                if p["kind"] == "single":
                    out.add(p["code"])
                else:
                    out.update(l["code"] for l in p["legs"])
        return out

    # ------------------------------------------------------------- entries
    async def on_rule_fired(self, rule: Rule) -> None:
        act = rule.order_action
        if not act or not act.get("enabled"):
            return
        if not self.auto_enabled:
            self.olog.write("skip", rule_id=rule.id, rule=rule.name, reason="自动下单总开关关闭，规则已触发但未下单")
            return
        async with self._lock:
            try:
                await self._enter(rule)
            except UserError as e:
                self.olog.write("reject", rule_id=rule.id, rule=rule.name, reason=str(e))
                self.toast("error", f"自动下单未执行（{rule.name}）：{e}")
            self.publish()

    async def _enter(self, rule: Rule) -> None:
        self._rollover()
        L = self.limits()
        a = self.validate_action(rule.order_action, require_switch=True)
        if self.daily.get("loss_locked"):
            raise UserError("今日已达到最大亏损限制")
        if self.daily["entries"] >= int(L.get("max_orders_per_day", 0)):
            raise UserError(f"已达每日最大自动开仓次数 {L.get('max_orders_per_day')}（硬性限制）")
        ts = L.get("time_stop_et")
        if ts and now_et().time() >= parse_hhmm(ts):
            raise UserError(f"已过时间止损时间 {ts}（美东），不再自动开新仓")
        env, acc = self.env, self.acc_for(self.env)
        if acc is None:
            raise UserError(f"没有可用的{ENV_ZH[env]}账户")
        if not self.app.gw.trade_ready():
            raise UserError("交易连接未就绪")
        offset = float(L.get("mid_offset", 0) or 0)
        remark = f"auto:{rule.id}"[:64]
        try:
            if a["type"] == "combo":
                if env == "REAL" and not self.combo.get("paper_verified"):
                    raise UserError("组合单尚未在模拟盘成功验证，实盘禁止使用组合单")
                bid, ask, mid = await self._combo_mid(a["legs"])
                if mid <= 0:
                    raise UserError(f"组合中间价为 {mid:.2f}（非正），仅支持净借方组合自动下单")
                price = round_price(mid + offset)
                resp = await self.app.client.place_combo_order(legs=a["legs"], qty=a["quantity"], price=price,
                                                               env=env, acc_id=acc, remark=remark)
                contract = " / ".join(f"{l['side']} {l['ratio']}x {l['code']}" for l in a["legs"])
                side = "BUY"
            else:
                bid, ask, mid = await self._fresh_mid(a["contract"])
                side = a["side"]
                price = round_price(mid + offset if side == "BUY" else mid - offset)
                resp = await self.app.client.place_order(code=a["contract"], side=side, qty=a["quantity"],
                                                         price=price, env=env, acc_id=acc, remark=remark)
                contract = a["contract"]
        except OpenDError as e:
            self.olog.write("place_failed", role="entry", env=env, account=mask(acc), rule_id=rule.id, rule=rule.name,
                            contract=a.get("contract") or a.get("legs"), side=a.get("side", "BUY"),
                            qty=a["quantity"], raw=e.msg)
            hint = ""
            if a["type"] == "combo" and any(k in e.msg.lower() for k in ("not support", "不支持", "unsupported")):
                self.combo.update({"status": "unsupported", "message": e.msg})
                hint = "（已标记账户不支持组合单）"
            if env == "REAL" and any(k in e.msg.lower() for k in ("unlock", "解锁")):
                hint = "（实盘交易未解锁：请在 OpenD 界面手动解锁交易）"
            raise UserError(f"下单失败：{e.msg}{hint}") from None
        rec = resp[0] if resp else {}
        oid = str(rec.get("order_id", ""))
        self.olog.write("place", role="entry", env=env, account=mask(acc), rule_id=rule.id, rule=rule.name,
                        contract=contract, side=side, qty=a["quantity"], price=price,
                        quote={"bid": bid, "ask": ask, "mid": round(mid, 4), "offset": offset}, order_id=oid, raw=resp)
        if rec:
            self._ingest_order(rec, "place")
        pid = uuid.uuid4().hex[:10]
        self.positions[pid] = {
            "id": pid, "rule_id": rule.id, "rule_name": rule.name, "kind": a["type"],
            "code": a.get("contract"), "legs": a.get("legs"), "side": side, "qty": a["quantity"], "env": env,
            "acc_id": acc, "status": "entry_pending", "entry_order_id": oid, "entry_limit": price,
            "entry_price": None, "filled_qty": 0.0, "created_ts": time.time(), "created_et": et_str(),
            "exit_order_id": None, "exit_reason": None, "exit_ts": None, "exit_price": None, "pnl": None,
            "note": "", "halted": False, "exit_failures": 0}
        self.daily["entries"] += 1
        self._save()
        if not self.auto_enabled:  # emergency stop / loss limit hit while the order was in flight
            self.positions[pid].update(halted=True, note="下单期间自动下单被关闭，已立即撤单")
            await self._cancel(oid, env, acc, "下单期间自动下单被关闭（紧急停止），立即撤单")
            return
        self.toast("info", f"已自动下单（{ENV_ZH[env]}）：{side} {a['quantity']} 张 {contract} @ {price:.2f}")

    # ------------------------------------------------------------- pushes
    def _rule_of(self, oid: str) -> str | None:
        for p in self.positions.values():
            if oid in (p.get("entry_order_id"), p.get("exit_order_id")):
                return p["rule_name"]
        return None

    def _ingest_order(self, r: dict, source: str) -> None:
        oid = str(r.get("order_id") or "")
        if not oid:
            return
        prev = self.orders.get(oid)
        merged = {**(prev or {}), **{k: v for k, v in r.items() if v is not None}}
        self.orders[oid] = merged
        if source != "place" and (prev is None or prev.get("order_status") != merged.get("order_status")
                                  or prev.get("dealt_qty") != merged.get("dealt_qty")):
            self.olog.write("order_update", source=source, order_id=oid, contract=merged.get("code"),
                            side=merged.get("trd_side"), qty=merged.get("qty"), price=merged.get("price"),
                            status=merged.get("order_status"), dealt_qty=merged.get("dealt_qty"),
                            dealt_avg_price=merged.get("dealt_avg_price"), env=merged.get("trd_env"),
                            rule=self._rule_of(oid), raw=r)
        for p in self.positions.values():
            if p["kind"] == "combo" and p["env"] == "SIMULATE" and p.get("entry_order_id") == oid \
                    and merged.get("order_status") in ("SUBMITTED", "FILLED_PART", "FILLED_ALL"):
                if not self.combo.get("paper_verified"):
                    self.combo.update({"status": "supported", "paper_verified": True,
                                       "message": f"模拟盘组合单已受理（{et_str()}）"})
                    self._save()

    def on_order_push(self, records: list[dict]) -> None:
        for r in records or []:
            self._ingest_order(r, "push")
        self.publish()

    def on_deal_push(self, records: list[dict]) -> None:
        for d in records or []:
            oid = str(d.get("order_id") or "")
            self.olog.write("fill", order_id=oid, deal_id=d.get("deal_id"), contract=d.get("code"),
                            side=d.get("trd_side"), qty=d.get("qty"), price=d.get("price"),
                            env=d.get("trd_env"), rule=self._rule_of(oid), raw=d)
        self.publish()

    # -------------------------------------------------------- management
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._tick()
            except Exception:
                log.exception("trading tick failed")

    async def _tick(self) -> None:
        self._rollover()
        active = [p for p in self.positions.values() if p["status"] in ACTIVE]
        if not active:
            return
        pending = [p for p in active if p["status"] in ("entry_pending", "exit_pending")]
        if pending and time.monotonic() - self._last_poll >= 5 and self.app.gw.trade_ready():
            self._last_poll = time.monotonic()
            for env in {p["env"] for p in pending}:
                acc = self.acc_for(env)
                if acc is None:
                    continue
                try:
                    for r in await self.app.client.order_list(env=env, acc_id=acc, refresh=False):
                        self._ingest_order(r, "poll")
                except OpenDError as e:
                    log.debug("order poll failed: %s", e)
        async with self._lock:
            for p in active:
                await self._manage(p)
            await self._check_daily_loss()
        self.publish()

    async def _manage(self, p: dict) -> None:
        now = time.time()
        L = self.limits()
        if p["status"] == "entry_pending":
            o = self.orders.get(p["entry_order_id"])
            if not o:
                return
            st, filled = o.get("order_status"), float(o.get("dealt_qty") or 0)
            if st == "FILLED_ALL" or (st in FINAL_STATUSES and filled > 0):
                p.update(status="unmanaged" if p.get("halted") else "open",
                         entry_price=float(o.get("dealt_avg_price") or p["entry_limit"]), filled_qty=filled,
                         opened_et=et_str())
                self.olog.write("position_open", position=p["id"], rule=p["rule_name"],
                                contract=p.get("code") or p.get("legs"), side=p["side"], qty=filled,
                                price=p["entry_price"], env=p["env"])
                self.toast("info", f"入场成交：{p.get('code') or '组合'} {filled:g} 张 @ {p['entry_price']:.2f}")
                self._save()
            elif st in FINAL_STATUSES:
                p.update(status="cancelled", note=f"入场单未成交（{st}）")
                self._save()
            elif (self.auto_enabled and not p.get("entry_cancel_sent")
                  and now - p["created_ts"] > float(L.get("entry_timeout_sec", 60))):
                p["entry_cancel_sent"] = True
                await self._cancel(p["entry_order_id"], p["env"], p["acc_id"], "入场单超时未成交，自动撤单", p)
            return

        if p["status"] == "open":
            if p.get("halted"):
                p["status"] = "unmanaged"
                return
            if not self.auto_enabled:
                return
            mark = self._mark(p)
            entry = p.get("entry_price")
            if mark is None or not entry:
                return
            sign = 1 if p["side"] == "BUY" else -1
            pnl_pct = (mark - entry) / abs(entry) * 100 * sign
            reason = None
            if pnl_pct <= -float(L.get("stop_loss_pct", 100)):
                reason = f"止损（{pnl_pct:.1f}%）"
            elif pnl_pct >= float(L.get("take_profit_pct", 1e9)):
                reason = f"止盈（{pnl_pct:.1f}%）"
            elif L.get("time_stop_et") and now_et().time() >= parse_hhmm(L["time_stop_et"]):
                reason = f"时间止损（{L['time_stop_et']} 美东）"
            if reason:
                await self._submit_exit(p, reason)
            return

        if p["status"] == "exit_pending":
            o = self.orders.get(p.get("exit_order_id") or "")
            if not o:
                return
            st, filled = o.get("order_status"), float(o.get("dealt_qty") or 0)
            if st in FINAL_STATUSES and filled > 0:
                px = float(o.get("dealt_avg_price") or 0)
                sign = 1 if p["side"] == "BUY" else -1
                pnl = round((px - p["entry_price"]) * filled * 100 * sign, 2)
                self.daily["realized"] += pnl
                p["pnl"] = round((p.get("pnl") or 0) + pnl, 2)
                p["filled_qty"] = round(p["filled_qty"] - filled, 6)
                self.olog.write("position_closed" if p["filled_qty"] <= 0 else "position_reduced",
                                position=p["id"], rule=p["rule_name"], reason=p.get("exit_reason"),
                                contract=p.get("code") or p.get("legs"), qty=filled, price=px, pnl=pnl, env=p["env"])
                if p["filled_qty"] <= 0:
                    p.update(status="closed", exit_price=px)
                    self.toast("info", f"已平仓（{p.get('exit_reason')}）：盈亏 {pnl:+.2f} 美元")
                else:
                    p.update(status="open", exit_order_id=None)
                self._save()
            elif st in FINAL_STATUSES:
                p.update(status="unmanaged" if p.get("halted") else "open", exit_order_id=None)
            elif (self.auto_enabled and not p.get("exit_cancel_sent")
                  and now - (p.get("exit_ts") or now) > float(L.get("exit_reprice_sec", 20))):
                p["exit_cancel_sent"] = True
                await self._cancel(p["exit_order_id"], p["env"], p["acc_id"], "平仓单未成交，撤单后按新中间价重新报价", p)

    async def _submit_exit(self, p: dict, reason: str) -> None:
        L = self.limits()
        offset = float(L.get("mid_offset", 0) or 0)
        acc, env = p["acc_id"], p["env"]
        remark = f"auto-exit:{p['rule_id']}"[:64]
        try:
            if p["kind"] == "combo":
                legs = [{**l, "side": "SELL" if l["side"] == "BUY" else "BUY"} for l in p["legs"]]
                bid, ask, mid = await self._combo_mid(legs)
                if mid <= 0:
                    p.update(status="unmanaged", note=f"组合平仓价 {mid:.2f} 非正，无法自动平仓，请手动处理")
                    self.olog.write("exit_unmanaged", position=p["id"], reason=p["note"])
                    self.toast("error", p["note"])
                    return
                price = round_price(mid + offset)
                resp = await self.app.client.place_combo_order(legs=legs, qty=p["filled_qty"], price=price,
                                                               env=env, acc_id=acc, remark=remark)
                side, contract = "BUY", " / ".join(f"{l['side']} {l['ratio']}x {l['code']}" for l in legs)
            else:
                side = "SELL" if p["side"] == "BUY" else "BUY"
                bid, ask, mid = await self._fresh_mid(p["code"])
                price = round_price(mid - offset if side == "SELL" else mid + offset)
                resp = await self.app.client.place_order(code=p["code"], side=side, qty=p["filled_qty"], price=price,
                                                         env=env, acc_id=acc, remark=remark)
                contract = p["code"]
        except (OpenDError, UserError) as e:
            msg = getattr(e, "msg", str(e))
            p["exit_failures"] = p.get("exit_failures", 0) + 1
            self.olog.write("place_failed", role="exit", reason=reason, position=p["id"], rule=p["rule_name"],
                            contract=p.get("code") or p.get("legs"), qty=p["filled_qty"], env=env, raw=msg)
            if p["exit_failures"] >= 3:
                p.update(status="unmanaged", note=f"自动平仓连续失败：{msg}")
                self.toast("error", f"自动平仓连续失败，请手动处理：{msg}")
            self._save()
            return
        rec = resp[0] if resp else {}
        oid = str(rec.get("order_id", ""))
        self.olog.write("place", role="exit", reason=reason, env=env, account=mask(acc), rule_id=p["rule_id"],
                        rule=p["rule_name"], contract=contract, side=side, qty=p["filled_qty"], price=price,
                        quote={"bid": bid, "ask": ask, "mid": round(mid, 4), "offset": offset}, order_id=oid, raw=resp)
        if rec:
            self._ingest_order(rec, "place")
        p.update(status="exit_pending", exit_order_id=oid, exit_reason=reason, exit_ts=time.time(),
                 exit_cancel_sent=False)
        self._save()
        if p.get("halted") or not self.auto_enabled:  # emergency stop while the exit was in flight
            await self._cancel(oid, env, acc, "平仓单提交期间触发紧急停止，立即撤单")

    async def _cancel(self, oid: str, env: str, acc: int, reason: str, p: dict | None = None) -> bool:
        try:
            resp = await self.app.client.cancel_order(order_id=oid, env=env, acc_id=acc)
            self.olog.write("cancel", order_id=oid, env=env, reason=reason, rule=self._rule_of(oid),
                            contract=(self.orders.get(oid) or {}).get("code"), raw=resp)
            return True
        except OpenDError as e:
            self.olog.write("cancel_failed", order_id=oid, env=env, reason=reason, rule=self._rule_of(oid), raw=e.msg)
            return False

    async def _check_daily_loss(self) -> None:
        dv = self.daily_view()
        max_loss = float(self.limits().get("max_daily_loss_usd", 0) or 0)
        if not self.auto_enabled or max_loss <= 0 or dv["total"] > -max_loss:
            return
        self.auto_enabled = False
        self.daily["loss_locked"] = True
        self.olog.write("auto_off", reason="达到每日最大亏损", total=dv["total"], limit=max_loss)
        for p in self.positions.values():
            if p["status"] == "entry_pending":
                await self._cancel(p["entry_order_id"], p["env"], p["acc_id"], "达到每日最大亏损，撤销入场单", p)
        self._save()
        self.toast("error", f"当日亏损 {dv['total']:.2f} 美元已达上限 {max_loss:g}，自动下单已关闭。"
                            f"已有持仓不会再自动平仓，请手动处理。")

    # ------------------------------------------------------ emergency stop
    async def emergency_stop(self) -> dict:
        was_on = self.auto_enabled
        self.auto_enabled = False
        for p in self.positions.values():
            if p["status"] in ACTIVE:
                p["halted"] = True
                p["note"] = "紧急停止：不再自动管理，请手动处理"
                if p["status"] == "open":
                    p["status"] = "unmanaged"
        self.olog.write("emergency_stop", env=self.env, auto_was_on=was_on)
        self.publish()
        summary = {"cancelled": 0, "failed": 0, "envs": []}
        envs = {self.env} | {p["env"] for p in self.positions.values() if p.get("halted") and p["status"] in ACTIVE}
        for env in envs:
            acc = self.acc_for(env)
            if acc is None:
                continue
            if env == "REAL":
                try:
                    resp = await self.app.client.cancel_all_order(env=env, acc_id=acc)
                    self.olog.write("cancel_all", env=env, account=mask(acc), raw=resp)
                except OpenDError as e:
                    self.olog.write("cancel_all_failed", env=env, account=mask(acc), raw=e.msg)
            try:
                recs = await self.app.client.order_list(env=env, acc_id=acc, refresh=True, priority=Priority.CRITICAL)
            except OpenDError as e:
                self.olog.write("order_list_failed", env=env, raw=e.msg)
                recs = [o for o in self.orders.values() if o.get("trd_env") == env]
            for r in recs:
                self._ingest_order(r, "emergency")
            for r in recs:
                if r.get("order_status") in OPEN_STATUSES:
                    ok = await self._cancel(str(r["order_id"]), env, acc, "紧急停止")
                    summary["cancelled" if ok else "failed"] += 1
            summary["envs"].append(env)
        self._save()
        text = f"紧急停止完成：自动下单已关闭，已撤销 {summary['cancelled']} 笔挂单"
        if summary["failed"]:
            text += f"，{summary['failed']} 笔撤单失败，请在 moomoo 中检查"
        self.toast("warn" if summary["failed"] else "info", text)
        self.publish()
        return summary

    # ---------------------------------------------------------- combo check
    async def check_combo(self, legs_raw: list[dict]) -> dict:
        """Ask OpenD (paper account, no order placed) whether this combo is accepted."""
        legs = self.validate_legs(legs_raw)
        acc = self.acc_for("SIMULATE")
        if acc is None:
            raise UserError("没有可用的模拟账户")
        try:
            bid, ask, mid = await self._combo_mid(legs)
            resp = await self.app.client.combo_trading_info(legs=legs, qty=1, price=round_price(abs(mid)),
                                                            env="SIMULATE", acc_id=acc)
            self.combo.update({"status": "supported", "message": f"模拟盘检测通过（{et_str()}）"})
            self.olog.write("combo_check", ok=True, legs=legs, raw=resp)
        except OpenDError as e:
            self.combo.update({"status": "unsupported", "message": e.msg})
            self.olog.write("combo_check", ok=False, legs=legs, raw=e.msg)
        self._save()
        self.publish()
        return self.combo
