"""Config file access.

One YAML file holds everything (OpenD address, rate limits, watchlist, option
settings, alert rules, auto-trading hard limits). ruamel.yaml round-trip mode
keeps the user's comments when the UI writes changes back.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "opend": {"host": "127.0.0.1", "port": 11111, "telnet_port": 22222,
              "security_firm": "FUTUINC", "ai_type": 1},
    "server": {"port": 8600},
    "rate_limiter": {"safety_margin_sec": 0.25},
    "rate_limits": {
        "get_market_snapshot": {"max_calls": 60, "window_sec": 30, "max_codes": 400, "reserve_high": 6,
                                "consumer_caps": {"market": 30}},
        "get_plate_list": {"max_calls": 10, "window_sec": 30},
        "get_plate_stock": {"max_calls": 10, "window_sec": 30},
        "get_capital_flow": {"max_calls": 30, "window_sec": 30, "consumer_caps": {"market": 24}},
        "get_search_news": {"max_calls": 10, "window_sec": 30},
        "get_earnings_calendar": {"max_calls": 60, "window_sec": 30},
        "get_option_chain": {"max_calls": 10, "window_sec": 30, "max_span_days": 30},
        "get_option_expiration_date": {"max_calls": 60, "window_sec": 30},
        "place_order": {"max_calls": 15, "window_sec": 30, "min_interval_sec": 0.02},
        "place_combo_order": {"bucket": "place_order"},
        "modify_order": {"max_calls": 20, "window_sec": 30, "min_interval_sec": 0.04},
        "cancel_all_order": {"bucket": "modify_order"},
        "order_list_query": {"max_calls": 10, "window_sec": 30},
        "request_highest_quote_right": {"max_calls": 10, "window_sec": 60},
        # 个股分析
        "get_search_quote": {"max_calls": 10, "window_sec": 30},
        "get_owner_plate": {"max_calls": 10, "window_sec": 30},
        "get_company_profile": {"max_calls": 10, "window_sec": 30},
        "get_financials_statements": {"max_calls": 10, "window_sec": 30},
        "get_valuation_detail": {"max_calls": 10, "window_sec": 30},
        "get_research_analyst_consensus": {"max_calls": 10, "window_sec": 30},
        "get_financials_earnings_price_move": {"max_calls": 10, "window_sec": 30},
        "get_option_volatility": {"max_calls": 10, "window_sec": 30},
    },
    "quota": {"min_subscription_hold_sec": 60},
    "stocks": {"subscribe_session": "ALL", "snapshot_refresh_sec": 5, "watchlist": []},
    "options": {"default_interval_sec": 5, "min_interval_sec": 1, "default_strikes_each_side": 8,
                "pinned_refresh_sec": 5, "subscription_subtypes": ["QUOTE", "ORDER_BOOK"],
                "underlyings": [], "pinned": []},
    "alerts": [],
    "auto_trading": {
        "limits": {"max_contracts_per_order": 1, "max_orders_per_day": 5, "max_daily_loss_usd": 200,
                   "stop_loss_pct": 30, "take_profit_pct": 50, "time_stop_et": "15:45",
                   "mid_offset": 0.0, "entry_timeout_sec": 60, "exit_reprice_sec": 20},
        "real_acc_id": None,
    },
    "mock": {"kline_5m_seconds": 300, "combo_supported": True},
    "market": {"plate_set": "INDUSTRY", "benchmarks": ["US.SPY", "US.QQQ", "US.TQQQ", "US.DIA", "US.SOXX"],
               "min_market_cap_usd": 2e9, "min_dollar_volume_usd": 2e7, "refresh_sec": 60,
               "capital_flow_refresh_sec": 300, "capital_flow_calls_per_cycle": 200,
               "capital_flow_method": "constituents", "top_n_sectors": 5, "top_n_flows": 10, "top_n_leaders": 5,
               "us_only": True, "exclude_codes": []},
    "news": {"contact_email": "", "app_name": "moomoo-local-dashboard",
             "user_agent": "moomoo-local-dashboard/1.0 (news monitor)",
             "sources": {"moomoo_search": {"enabled": True, "poll_interval_sec": 120, "news_types": [1, 2],
                                           "lang": "en", "size": 20},
                         "moomoo_api": {"enabled": True, "poll_interval_sec": 300, "max_count": 20},
                         "sec": {"enabled": True, "poll_interval_sec": 60, "max_requests_per_sec": 5,
                                 "include_form4": False, "initial_lookback_days": 2},
                         "earnings": {"enabled": True, "refresh_sec": 1800, "days_ahead": 7}},
             "http_rate_limits": {"moomoo_news_search": {"max_calls": 20, "window_sec": 60}},
             "extra_keywords": [], "notify_medium": False, "notify_max_age_min": 120,
             "dedup": {"window_min": 30, "similarity": 0.5}, "retention_days": 30, "mock_interval_sec": 20,
             "layer2": {"enabled": False, "model": "claude-opus-5-5", "effort": "low", "daily_cap": 300,
                        "max_summary_chars": 500, "fallbacks": "default"}},
    "stock_analysis": {"enabled": False, "model": "claude-opus-5-5", "effort": "high", "fallbacks": "default", "max_tokens": 64000,
                       "web_search_max_uses": 20, "max_continuations": 5, "daily_cap": 50, "cache_minutes": 30,
                       "max_concurrent": 2, "request_wait_sec": 240,
                       "max_option_days": 7, "min_open_interest": 500, "max_spread_pct": 10,
                       "chain_strike_window_pct": 20, "chain_max_strikes_each_side": 30},
}


def to_plain(obj: Any) -> Any:
    """Convert ruamel containers to plain dict/list (deep)."""
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


def to_yaml_node(obj: Any) -> Any:
    """Convert plain containers to ruamel containers so they dump in block style."""
    if isinstance(obj, dict):
        m = CommentedMap()
        for k, v in obj.items():
            m[k] = to_yaml_node(v)
        return m
    if isinstance(obj, (list, tuple)):
        s = CommentedSeq()
        for v in obj:
            s.append(to_yaml_node(v))
        # keep short scalar lists (e.g. subtypes, time windows) on one line
        if all(not isinstance(v, (dict, list, tuple)) for v in obj) and len(obj) <= 8:
            s.fa.set_flow_style()
        return s
    if isinstance(obj, str) and ":" in obj:
        # "09:30" must stay a string for every YAML reader (YAML 1.1 would read 10:30 as 630)
        return DoubleQuotedScalarString(obj)
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigStore:
    """Thread-safe config holder. `data` is a plain dict merged with defaults."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.width = 120
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._lock = threading.RLock()
        self._doc: CommentedMap = CommentedMap()
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as f:
                    doc = self._yaml.load(f) or CommentedMap()
            else:
                doc = CommentedMap()
            self._doc = doc
            self.data = _deep_merge(DEFAULTS, to_plain(doc))
            # rate_limits: user values replace defaults per endpoint (not merged per key)
            if "rate_limits" in doc:
                merged = copy.deepcopy(DEFAULTS["rate_limits"])
                merged.update(to_plain(doc["rate_limits"]))
                self.data["rate_limits"] = merged

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self.data
            for k in keys:
                if not isinstance(node, dict) or k not in node:
                    return default
                node = node[k]
            return copy.deepcopy(node)

    def set(self, keys: tuple[str, ...], value: Any, save: bool = True) -> None:
        """Set a value (plain python) at a key path, mirror it into the YAML doc, save."""
        with self._lock:
            node = self.data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = copy.deepcopy(value)

            doc = self._doc
            for k in keys[:-1]:
                if k not in doc or not isinstance(doc[k], dict):
                    doc[k] = CommentedMap()
                doc = doc[k]
            doc[keys[-1]] = to_yaml_node(value)
            if save:
                self.save()

    def set_item(self, keys: tuple, value: Any, save: bool = True) -> None:
        """Set one leaf (keys may contain list indexes) in place, keeping surrounding comments."""
        with self._lock:
            node, doc = self.data, self._doc
            for k in keys[:-1]:
                node, doc = node[k], doc[k]
            node[keys[-1]] = copy.deepcopy(value)
            doc[keys[-1]] = to_yaml_node(value)
            if save:
                self.save()

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                self._yaml.dump(self._doc, f)
            os.replace(tmp, self.path)
            log.info("config saved: %s", self.path)
