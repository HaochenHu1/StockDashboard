"""Alert rule engine - pure logic, no I/O, so it can be unit tested.

A rule watches one symbol (stock, ETF or option contract). All of its
conditions are combined with AND; a condition may name another symbol (for
example an option rule that also requires the underlying to be in a range).

Semantics
* "above" means >= and "below" means <=, so "5 minute close not below 674"
  is written as kline_5m_close_above 674.
* kline_5m_close uses the most recent *completed* 5 minute candle.
* A rule may be limited to a US Eastern time window [start, end); windows that
  cross midnight are allowed.
* trigger_once rules fire a single time until re-armed. Other rules fire again
  after `cooldown_sec` while the conditions stay true.
* Missing data never satisfies a condition.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .timeutil import in_window, parse_hhmm

CONDITION_TYPES: dict[str, dict[str, str]] = {
    "price_above":          {"field": "last",     "op": "ge",    "label": "最新价 ≥"},
    "price_below":          {"field": "last",     "op": "le",    "label": "最新价 ≤"},
    "kline_5m_close_above": {"field": "k5_close", "op": "ge",    "label": "5 分钟 K 线收盘 ≥"},
    "kline_5m_close_below": {"field": "k5_close", "op": "le",    "label": "5 分钟 K 线收盘 ≤"},
    "price_in_range":       {"field": "last",     "op": "range", "label": "价格位于区间"},
    "option_mid_above":     {"field": "mid",      "op": "ge",    "label": "期权中间价 ≥"},
    "option_mid_below":     {"field": "mid",      "op": "le",    "label": "期权中间价 ≤"},
    "spread_wider_than":    {"field": "spread",   "op": "gt",    "label": "买卖价差 >"},
    "news_high_within":     {"field": "news_high_age_min", "op": "le", "label": "N 分钟内出现高重要性新闻"},
}
NEWS_CONDITION_TYPES = {"news_high_within"}
# value used when a symbol has no 高 news: the condition is then simply false (not "missing data")
NO_NEWS_AGE = 1e9

CODE_RE = re.compile(r"^US\.[A-Z0-9][A-Z0-9.\-]*$")


class RuleError(ValueError):
    """Validation error; the message is user-facing Chinese text."""


def normalize_code(code: Any) -> str:
    text = str(code or "").strip().upper().replace(" ", "")
    if text and "." not in text:
        text = "US." + text
    return text


def _num(value: Any, what: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise RuleError(f"{what}必须是数字") from None
    if not math.isfinite(f):
        raise RuleError(f"{what}必须是有限数字")
    return f


@dataclass(frozen=True)
class Condition:
    type: str
    value: float | None = None
    low: float | None = None
    high: float | None = None
    symbol: str | None = None          # None = the rule's symbol

    def describe(self, rule_symbol: str = "") -> str:
        meta = CONDITION_TYPES[self.type]
        who = f"{self.symbol} " if self.symbol and self.symbol != rule_symbol else ""
        if meta["op"] == "range":
            return f"{who}价格位于 {self.low:g} ~ {self.high:g}"
        if self.type == "news_high_within":
            return f"{who}{self.value:g} 分钟内出现高重要性新闻"
        return f"{who}{meta['label']} {self.value:g}"


@dataclass
class Rule:
    id: str
    name: str
    symbol: str
    conditions: list[Condition]
    enabled: bool = True
    window: tuple[dt.time, dt.time] | None = None
    trigger_once: bool = True
    cooldown_sec: float = 300.0
    order_action: dict | None = None

    def symbols(self) -> set[str]:
        return {c.symbol or self.symbol for c in self.conditions} | {self.symbol}

    def news_only(self) -> bool:
        """True when every condition is a news condition (such rules may not carry an order action)."""
        return all(c.type in NEWS_CONDITION_TYPES for c in self.conditions)

    def needs(self) -> dict[str, set[str]]:
        """symbol -> data fields the conditions need."""
        out: dict[str, set[str]] = {}
        for c in self.conditions:
            out.setdefault(c.symbol or self.symbol, set()).add(CONDITION_TYPES[c.type]["field"])
        return out


@dataclass
class RuleState:
    fired_count: int = 0
    last_fired_ts: float | None = None
    done: bool = False


@dataclass
class FireEvent:
    rule: Rule
    ts: float
    et_time: str
    values: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def message(self) -> str:
        parts = [c.describe(self.rule.symbol) for c in self.rule.conditions]
        return f"{self.rule.name}：{self.rule.symbol} 满足条件（{'，且 '.join(parts)}）"


# --------------------------------------------------------------------- parse
def parse_condition(d: Mapping[str, Any], idx: int) -> Condition:
    if not isinstance(d, Mapping):
        raise RuleError(f"第 {idx} 个条件格式错误")
    ctype = str(d.get("type", "")).strip()
    if ctype not in CONDITION_TYPES:
        raise RuleError(f"第 {idx} 个条件类型未知：{ctype or '（空）'}")
    sym = d.get("symbol")
    sym = normalize_code(sym) if sym else None
    if sym and not CODE_RE.match(sym):
        raise RuleError(f"第 {idx} 个条件的代码格式错误：{sym}")
    if CONDITION_TYPES[ctype]["op"] == "range":
        low = _num(d.get("low"), f"第 {idx} 个条件的下限")
        high = _num(d.get("high"), f"第 {idx} 个条件的上限")
        if not low < high:
            raise RuleError(f"第 {idx} 个条件：区间下限必须小于上限")
        return Condition(ctype, low=low, high=high, symbol=sym)
    value = _num(d.get("value"), f"第 {idx} 个条件的数值")
    if ctype == "news_high_within" and not 1 <= value <= 1440:
        raise RuleError(f"第 {idx} 个条件：分钟数应在 1 到 1440 之间")
    if ctype == "spread_wider_than" and value < 0:
        raise RuleError(f"第 {idx} 个条件：价差不能为负数")
    return Condition(ctype, value=value, symbol=sym)


def parse_window(raw: Any) -> tuple[dt.time, dt.time] | None:
    if raw in (None, "", [], ()):
        return None
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.replace("~", "-").split("-")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise RuleError("生效时段应为 [开始, 结束]，例如 09:30 到 10:30")
    try:
        return parse_hhmm(raw[0]), parse_hhmm(raw[1])
    except (ValueError, TypeError):
        raise RuleError("生效时段格式错误，应为 HH:MM（美东时间）") from None


def parse_rule(d: Mapping[str, Any]) -> Rule:
    if not isinstance(d, Mapping):
        raise RuleError("规则格式错误")
    symbol = normalize_code(d.get("symbol"))
    if not CODE_RE.match(symbol):
        raise RuleError(f"标的代码格式错误：{symbol or '（空）'}，应为 moomoo 格式，例如 US.QQQ")
    conds_raw = d.get("conditions") or []
    if not isinstance(conds_raw, list) or not conds_raw:
        raise RuleError("至少需要一个条件")
    conditions = [parse_condition(c, i + 1) for i, c in enumerate(conds_raw)]
    trigger_once = bool(d.get("trigger_once", True))
    cooldown = _num(d.get("cooldown_sec", 300), "冷却时间")
    if not trigger_once and cooldown < 1:
        raise RuleError("重复触发的规则冷却时间至少 1 秒")
    name = str(d.get("name") or "").strip() or symbol
    order_action = d.get("order_action")
    return Rule(
        id=str(d.get("id") or ""),
        name=name,
        symbol=symbol,
        conditions=conditions,
        enabled=bool(d.get("enabled", True)),
        window=parse_window(d.get("active_window_et")),
        trigger_once=trigger_once,
        cooldown_sec=cooldown,
        order_action=dict(order_action) if isinstance(order_action, Mapping) else None,
    )


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    conds = []
    for c in rule.conditions:
        item: dict[str, Any] = {"type": c.type}
        if c.symbol:
            item["symbol"] = c.symbol
        if CONDITION_TYPES[c.type]["op"] == "range":
            item["low"], item["high"] = c.low, c.high
        else:
            item["value"] = c.value
        conds.append(item)
    out: dict[str, Any] = {"id": rule.id, "name": rule.name, "symbol": rule.symbol, "enabled": rule.enabled}
    if rule.window:
        out["active_window_et"] = [rule.window[0].strftime("%H:%M"), rule.window[1].strftime("%H:%M")]
    out["conditions"] = conds
    out["trigger_once"] = rule.trigger_once
    out["cooldown_sec"] = rule.cooldown_sec
    if rule.order_action is not None:
        out["order_action"] = dict(rule.order_action)
    return out


# ------------------------------------------------------------------ evaluate
def field_value(q: Mapping[str, Any] | None, name: str) -> float | None:
    if not q:
        return None
    v = q.get(name)
    if v is None and name in ("mid", "spread"):
        bid, ask = q.get("bid"), q.get("ask")
        if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
            return None
        v = (bid + ask) / 2 if name == "mid" else ask - bid
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # round away binary noise (1.10 - 1.00 = 0.10000000000000009) so thresholds compare as written
    return round(f, 6) if math.isfinite(f) else None


def evaluate_condition(cond: Condition, q: Mapping[str, Any] | None) -> bool | None:
    """True/False, or None when the needed data is missing."""
    meta = CONDITION_TYPES[cond.type]
    v = field_value(q, meta["field"])
    if v is None:
        return None
    op = meta["op"]
    if op == "ge":
        return v >= cond.value
    if op == "le":
        return v <= cond.value
    if op == "gt":
        return v > cond.value
    return cond.low <= v <= cond.high


def in_rule_window(rule: Rule, now_et: dt.datetime) -> bool:
    return rule.window is None or in_window(now_et.time(), rule.window[0], rule.window[1])


class AlertEngine:
    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.states: dict[str, RuleState] = {}

    def set_rules(self, rules: list[Rule]) -> None:
        self.rules = {r.id: r for r in rules}
        for rid in list(self.states):
            if rid not in self.rules:
                del self.states[rid]
        for rid in self.rules:
            self.states.setdefault(rid, RuleState())

    def load_states(self, raw: Mapping[str, Mapping[str, Any]]) -> None:
        for rid, s in (raw or {}).items():
            if rid in self.rules:
                self.states[rid] = RuleState(int(s.get("fired_count", 0)), s.get("last_fired_ts"), bool(s.get("done", False)))

    def dump_states(self) -> dict[str, dict[str, Any]]:
        return {rid: {"fired_count": s.fired_count, "last_fired_ts": s.last_fired_ts, "done": s.done}
                for rid, s in self.states.items()}

    def rearm(self, rule_id: str) -> None:
        if rule_id in self.rules:
            self.states[rule_id] = RuleState()

    def status(self, rule_id: str, quotes: Mapping[str, Mapping[str, Any]], now_et: dt.datetime,
               now_ts: float) -> str:
        """disabled | fired | outside_window | cooling | no_data | watching | matched"""
        rule = self.rules[rule_id]
        st = self.states.setdefault(rule_id, RuleState())
        if not rule.enabled:
            return "disabled"
        if rule.trigger_once and st.done:
            return "fired"
        if not in_rule_window(rule, now_et):
            return "outside_window"
        if (not rule.trigger_once and st.last_fired_ts is not None
                and now_ts - st.last_fired_ts < rule.cooldown_sec):
            return "cooling"
        results = [evaluate_condition(c, quotes.get(c.symbol or rule.symbol)) for c in rule.conditions]
        if any(r is None for r in results):
            return "no_data"
        return "matched" if all(results) else "watching"

    def evaluate(self, quotes: Mapping[str, Mapping[str, Any]], now_et: dt.datetime,
                 now_ts: float) -> list[FireEvent]:
        events: list[FireEvent] = []
        for rid, rule in self.rules.items():
            if self.status(rid, quotes, now_et, now_ts) != "matched":
                continue
            st = self.states[rid]
            st.fired_count += 1
            st.last_fired_ts = now_ts
            if rule.trigger_once:
                st.done = True
            values = {}
            for sym, fields in rule.needs().items():
                values[sym] = {f: field_value(quotes.get(sym), f) for f in sorted(fields)}
            events.append(FireEvent(rule, now_ts, now_et.strftime("%H:%M:%S"), values))
        return events
