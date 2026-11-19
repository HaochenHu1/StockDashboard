import datetime as dt

import pytest

from backend.alerts import (AlertEngine, Condition, RuleError, evaluate_condition, normalize_code, parse_rule,
                            rule_to_dict)
from backend.timeutil import ET, in_window, us_session

QQQ_RULE = {
    "id": "r1", "name": "QQQ gap fill long", "symbol": "US.QQQ",
    "active_window_et": ["09:30", "10:30"],
    "conditions": [{"type": "price_in_range", "low": 674.5, "high": 676.0},
                   {"type": "kline_5m_close_above", "value": 674.0}],
    "trigger_once": True,
    "order_action": {"enabled": False, "side": "BUY", "contract": None, "quantity": 1, "limit_price": "mid"},
}


def et(h, m, s=0, day=10):
    return dt.datetime(2026, 11, day, h, m, s, tzinfo=ET)


def engine_with(*rules):
    e = AlertEngine()
    e.set_rules([parse_rule(r) for r in rules])
    return e


# ------------------------------------------------------------------- parsing
def test_parse_example_rule():
    r = parse_rule(QQQ_RULE)
    assert r.symbol == "US.QQQ"
    assert r.window == (dt.time(9, 30), dt.time(10, 30))
    assert [c.type for c in r.conditions] == ["price_in_range", "kline_5m_close_above"]
    assert r.needs() == {"US.QQQ": {"last", "k5_close"}}


@pytest.mark.parametrize("patch, fragment", [
    ({"conditions": []}, "至少需要一个条件"),
    ({"conditions": [{"type": "bogus", "value": 1}]}, "类型未知"),
    ({"conditions": [{"type": "price_in_range", "low": 5, "high": 5}]}, "下限必须小于上限"),
    ({"conditions": [{"type": "price_above"}]}, "必须是数字"),
    ({"conditions": [{"type": "spread_wider_than", "value": -1}]}, "不能为负数"),
    ({"symbol": "HK.00700"}, "格式错误"),
    ({"active_window_et": ["9:30"]}, "生效时段"),
    ({"active_window_et": ["25:00", "26:00"]}, "生效时段格式错误"),
    ({"trigger_once": False, "cooldown_sec": 0}, "冷却时间至少 1 秒"),
])
def test_parse_errors_are_chinese(patch, fragment):
    with pytest.raises(RuleError) as ei:
        parse_rule({**QQQ_RULE, **patch})
    assert fragment in str(ei.value)


def test_normalize_code():
    assert normalize_code(" qqq ") == "US.QQQ"
    assert normalize_code("us.nvda") == "US.NVDA"


def test_round_trip():
    r = parse_rule(QQQ_RULE)
    again = parse_rule(rule_to_dict(r))
    assert again.conditions == r.conditions and again.window == r.window and again.order_action == r.order_action


# ---------------------------------------------------------------- conditions
@pytest.mark.parametrize("ctype, value, q, expected", [
    ("price_above", 100, {"last": 100.0}, True),          # "above" is >=
    ("price_above", 100, {"last": 99.99}, False),
    ("price_below", 100, {"last": 100.0}, True),          # "below" is <=
    ("price_below", 100, {"last": 100.01}, False),
    ("kline_5m_close_above", 674, {"k5_close": 674.0}, True),   # "not below 674"
    ("kline_5m_close_below", 674, {"k5_close": 674.5}, False),
    ("option_mid_above", 2.0, {"bid": 1.9, "ask": 2.1}, True),  # mid computed from bid/ask
    ("option_mid_below", 2.0, {"bid": 2.0, "ask": 2.2}, False),
    ("spread_wider_than", 0.10, {"bid": 1.0, "ask": 1.10}, False),  # strict >
    ("spread_wider_than", 0.10, {"bid": 1.0, "ask": 1.15}, True),
])
def test_conditions(ctype, value, q, expected):
    assert evaluate_condition(Condition(ctype, value=value), q) is expected


def test_range_is_inclusive_and_missing_data_is_none():
    c = Condition("price_in_range", low=674.5, high=676.0)
    assert evaluate_condition(c, {"last": 674.5}) is True
    assert evaluate_condition(c, {"last": 676.0}) is True
    assert evaluate_condition(c, {"last": 676.01}) is False
    assert evaluate_condition(c, {}) is None
    assert evaluate_condition(c, None) is None
    assert evaluate_condition(Condition("option_mid_above", value=1), {"bid": 1.0, "ask": 0}) is None  # no ask


# ------------------------------------------------------------------- engine
def test_and_combination_qqq_example():
    e = engine_with(QQQ_RULE)
    ok = {"US.QQQ": {"last": 675.2, "k5_close": 674.0}}
    bad = {"US.QQQ": {"last": 675.2, "k5_close": 673.9}}
    assert e.evaluate(bad, et(9, 45), 1.0) == []
    fired = e.evaluate(ok, et(9, 45), 2.0)
    assert len(fired) == 1 and fired[0].rule.id == "r1"
    assert fired[0].values["US.QQQ"] == {"k5_close": 674.0, "last": 675.2}
    assert "满足条件" in fired[0].message()


def test_time_window_et():
    e = engine_with(QQQ_RULE)
    q = {"US.QQQ": {"last": 675.0, "k5_close": 675.0}}
    assert e.evaluate(q, et(9, 29, 59), 1.0) == []
    assert e.status("r1", q, et(10, 30), 1.0) == "outside_window"   # end is exclusive
    assert len(e.evaluate(q, et(9, 30), 2.0)) == 1


def test_window_across_midnight():
    assert in_window(dt.time(23, 0), dt.time(20, 0), dt.time(4, 0))
    assert in_window(dt.time(3, 59), dt.time(20, 0), dt.time(4, 0))
    assert not in_window(dt.time(4, 0), dt.time(20, 0), dt.time(4, 0))


def test_trigger_once_and_rearm():
    e = engine_with({**QQQ_RULE, "active_window_et": None})
    q = {"US.QQQ": {"last": 675.0, "k5_close": 675.0}}
    assert len(e.evaluate(q, et(11, 0), 1.0)) == 1
    assert e.evaluate(q, et(11, 0), 500.0) == []
    assert e.status("r1", q, et(11, 0), 500.0) == "fired"
    e.rearm("r1")
    assert len(e.evaluate(q, et(11, 0), 501.0)) == 1


def test_cooldown_refires_while_true():
    e = engine_with({**QQQ_RULE, "active_window_et": None, "trigger_once": False, "cooldown_sec": 60})
    q = {"US.QQQ": {"last": 675.0, "k5_close": 675.0}}
    assert len(e.evaluate(q, et(11, 0), 0.0)) == 1
    assert e.evaluate(q, et(11, 0), 30.0) == []
    assert e.status("r1", q, et(11, 0), 30.0) == "cooling"
    assert len(e.evaluate(q, et(11, 1), 61.0)) == 1
    assert e.states["r1"].fired_count == 2


def test_missing_data_and_disabled():
    e = engine_with({**QQQ_RULE, "active_window_et": None},
                    {**QQQ_RULE, "id": "r2", "active_window_et": None, "enabled": False})
    assert e.evaluate({"US.QQQ": {"last": 675.0}}, et(11, 0), 1.0) == []   # no 5m candle yet
    assert e.status("r1", {"US.QQQ": {"last": 675.0}}, et(11, 0), 1.0) == "no_data"
    fired = e.evaluate({"US.QQQ": {"last": 675.0, "k5_close": 675.0}}, et(11, 0), 2.0)
    assert [f.rule.id for f in fired] == ["r1"]


def test_option_rule_with_underlying_condition():
    rule = {"id": "o1", "name": "call cheap", "symbol": "US.QQQ261113C675000",
            "conditions": [{"type": "option_mid_below", "value": 3.0},
                           {"type": "price_in_range", "symbol": "us.qqq", "low": 674, "high": 676},
                           {"type": "spread_wider_than", "value": 0.5}]}
    e = engine_with(rule)
    q = {"US.QQQ261113C675000": {"bid": 1.0, "ask": 1.8}, "US.QQQ": {"last": 675.0}}
    assert parse_rule(rule).symbols() == {"US.QQQ261113C675000", "US.QQQ"}
    assert len(e.evaluate(q, et(12, 0), 1.0)) == 1


def test_state_persistence():
    e = engine_with(QQQ_RULE)
    e.evaluate({"US.QQQ": {"last": 675.0, "k5_close": 675.0}}, et(9, 31), 7.0)
    dumped = e.dump_states()
    e2 = engine_with(QQQ_RULE)
    e2.load_states(dumped)
    assert e2.states["r1"].done and e2.states["r1"].last_fired_ts == 7.0


def test_us_session_clock():
    assert us_session(et(8, 0)) == "PRE"
    assert us_session(et(9, 30)) == "RTH"
    assert us_session(et(16, 0)) == "AFTER"
    assert us_session(et(23, 5)) == "OVERNIGHT"
    assert us_session(et(2, 0, day=9)) == "OVERNIGHT"        # Monday 02:00 = Sunday night session
    assert us_session(et(12, 0, day=14)) == "CLOSED"         # Saturday
    assert us_session(et(21, 0, day=13)) == "CLOSED"         # Friday night
    assert us_session(et(21, 0, day=15)) == "OVERNIGHT"      # Sunday 21:00
