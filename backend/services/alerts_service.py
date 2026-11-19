"""Alert rules: persistence, evaluation loop, notifications, link to automatic orders."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..alerts import NO_NEWS_AGE, AlertEngine, Rule, RuleError, parse_rule, rule_to_dict
from ..errors import UserError
from ..gateway.base import index_hint, is_option_code
from ..timeutil import now_et

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)
STATUS_ZH = {"disabled": "已停用", "fired": "已触发（仅一次）", "outside_window": "不在生效时段", "cooling": "冷却中",
             "no_data": "等待数据", "watching": "监控中", "matched": "条件满足"}


class AlertService:
    def __init__(self, app: "AppState", data_dir: Path):
        self.app = app
        self.state_path = data_dir / "alert_state.json"
        self.engine = AlertEngine()
        self.invalid: list[dict] = []
        self.history: deque[dict] = deque(maxlen=100)
        self._task: asyncio.Task | None = None
        self._load()

    # ------------------------------------------------------------- persist
    def _load(self) -> None:
        raw_rules = self.app.cfg.get("alerts", default=[]) or []
        rules, changed = [], False
        self.invalid = []
        for i, raw in enumerate(raw_rules):
            if not raw.get("id"):
                raw["id"] = uuid.uuid4().hex[:8]
                self.app.cfg.set_item(("alerts", i, "id"), raw["id"], save=False)  # keeps YAML comments
                changed = True
            try:
                rules.append(parse_rule(raw))
            except RuleError as e:
                log.warning("alert rule %d invalid: %s", i, e)
                self.invalid.append({"raw": raw, "error": str(e)})
        if changed:
            self.app.cfg.save()
        self.engine.set_rules(rules)
        if self.state_path.exists():
            try:
                self.engine.load_states(json.loads(self.state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.exception("alert state unreadable")

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.engine.dump_states(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def _save_rules(self) -> None:
        rules = [rule_to_dict(r) for r in self.engine.rules.values()] + [x["raw"] for x in self.invalid]
        self.app.cfg.set(("alerts",), rules)
        self._save_state()
        self.publish()

    # -------------------------------------------------------------- views
    def rule_view(self, r: Rule) -> dict:
        d = rule_to_dict(r)
        st = self.engine.states.get(r.id)
        quotes = {s: self.quote_view(s) for s in r.symbols()}
        status = self.engine.status(r.id, quotes, now_et(), time.time())
        d["status"] = status
        d["status_zh"] = STATUS_ZH.get(status, status)
        d["fired_count"] = st.fired_count if st else 0
        d["last_fired_ts"] = st.last_fired_ts if st else None
        d["descriptions"] = [c.describe(r.symbol) for c in r.conditions]
        d["hints"] = self.data_hints(r)
        return d

    def data_hints(self, r: Rule) -> list[str]:
        hints = []
        news = getattr(self.app, "news", None)
        for sym, fields in r.needs().items():
            if "k5_close" in fields and not self.app.subs.active(sym, "K_5M"):
                hints.append(f"需要在“股票”页面为 {sym} 勾选 5 分钟 K 线订阅")
            if "news_high_age_min" in fields and news and sym not in news.watch_symbols():
                hints.append(f"{sym} 不在自选或期权标的中，资讯监控不会抓取它的新闻")
        return hints

    def quote_view(self, sym: str) -> dict:
        """Market fields for the rule engine plus the age of the latest 高 news item."""
        q = dict(self.app.store.alert_view(sym) or {})
        news = getattr(self.app, "news", None)
        age = news.high_age_min(sym) if news else None
        q["news_high_age_min"] = age if age is not None else NO_NEWS_AGE
        return q

    def list(self) -> dict:
        return {"rules": [self.rule_view(r) for r in self.engine.rules.values()], "invalid": self.invalid,
                "history": list(self.history)}

    def publish(self) -> None:
        self.app.hub.publish("alerts", self.list())

    def watched_codes(self) -> set[str]:
        out: set[str] = set()
        for r in self.engine.rules.values():
            if r.enabled:
                out |= r.symbols()
        return out

    # ------------------------------------------------------------ actions
    def _build(self, body: dict, rule_id: str) -> Rule:
        body = dict(body)
        body["id"] = rule_id
        try:
            rule = parse_rule(body)
        except RuleError as e:
            raise UserError(str(e)) from None
        for sym in rule.symbols():
            hint = index_hint(sym)
            if hint:
                raise UserError(hint)
        act = body.get("order_action")
        if act and act.get("enabled") and rule.news_only():
            raise UserError("只包含新闻条件的规则不能关联下单动作：新闻只说明发生了重要事件，不能单独作为下单依据。"
                            "请至少再添加一个价格条件（与新闻条件为“且”关系）")
        rule.order_action = self.app.trading.validate_action(act, require_switch=True)
        return rule

    def create(self, body: dict) -> dict:
        rule = self._build(body, uuid.uuid4().hex[:8])
        self.engine.set_rules(list(self.engine.rules.values()) + [rule])
        self._save_rules()
        return self.rule_view(rule)

    def update(self, rule_id: str, body: dict) -> dict:
        if rule_id not in self.engine.rules:
            raise UserError("规则不存在")
        old = self.engine.rules[rule_id]
        new_action = body.get("order_action")
        trading = self.app.trading
        if new_action and new_action.get("enabled") and not trading.auto_enabled:
            # an existing link may stay while the switch is off, but it cannot be added or changed
            norm_new = trading.validate_action(new_action, require_switch=False)
            norm_old = trading.validate_action(old.order_action, require_switch=False) if old.order_action else None
            if norm_old != norm_new:
                raise UserError("自动下单总开关关闭时，不能新增或修改规则的下单动作")
            rule = self._build({**body, "order_action": {**new_action, "enabled": False}}, rule_id)
            rule.order_action = norm_old
            if rule.news_only():
                raise UserError("只包含新闻条件的规则不能关联下单动作，请至少再添加一个价格条件")
        else:
            rule = self._build(body, rule_id)
        rules = [rule if r.id == rule_id else r for r in self.engine.rules.values()]
        self.engine.set_rules(rules)
        self._save_rules()
        return self.rule_view(rule)

    def delete(self, rule_id: str) -> dict:
        if rule_id not in self.engine.rules:
            raise UserError("规则不存在")
        self.engine.set_rules([r for r in self.engine.rules.values() if r.id != rule_id])
        self._save_rules()
        return {"deleted": rule_id}

    def rearm(self, rule_id: str) -> dict:
        if rule_id not in self.engine.rules:
            raise UserError("规则不存在")
        self.engine.rearm(rule_id)
        self._save_state()
        self.publish()
        return self.rule_view(self.engine.rules[rule_id])

    def set_enabled(self, rule_id: str, enabled: bool) -> dict:
        if rule_id not in self.engine.rules:
            raise UserError("规则不存在")
        self.engine.rules[rule_id].enabled = bool(enabled)
        self._save_rules()
        return self.rule_view(self.engine.rules[rule_id])

    # --------------------------------------------------------------- loop
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self._save_state()

    async def _loop(self) -> None:
        last_publish = 0.0
        while True:
            try:
                await asyncio.wait_for(self.app.store.changed.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.app.store.changed.clear()
            await asyncio.sleep(0.05)  # coalesce bursts of pushes
            try:
                self.app.store.refresh_candles()
                self._evaluate()
                if time.monotonic() - last_publish > 2:
                    last_publish = time.monotonic()
                    self.publish()
            except Exception:
                log.exception("alert evaluation failed")

    def _evaluate(self) -> None:
        symbols = self.watched_codes()
        if not symbols:
            return
        quotes = {s: self.quote_view(s) for s in symbols}
        events = self.engine.evaluate(quotes, now_et(), time.time())
        for ev in events:
            msg = ev.message()
            item = {"rule_id": ev.rule.id, "name": ev.rule.name, "symbol": ev.rule.symbol,
                    "symbols": sorted(ev.rule.symbols()), "time_et": ev.et_time, "ts": ev.ts, "message": msg,
                    "values": ev.values, "order_linked": bool(ev.rule.order_action and ev.rule.order_action.get("enabled"))}
            self.history.appendleft(item)
            log.info("ALERT %s", msg)
            self.app.hub.publish("alert", item)
            asyncio.create_task(self.app.trading.on_rule_fired(ev.rule))
        if events:
            self._save_state()
            self.publish()


def option_codes(codes: set[str]) -> set[str]:
    return {c for c in codes if is_option_code(c)}
