"""个股分析: trade plan checks, payoff math, JSON parsing, and the API flow in mock mode (no API calls)."""
import datetime as dt
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.analysis.llm import AnalysisLLM
from backend.analysis.validate import check_trade_plans, extract_json, normalize, payoff
from backend.app import create_app
from backend.config import ConfigStore
from backend.envfile import load_env
from backend.timeutil import now_et

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Dashboard": "1"}
TODAY = dt.date(2026, 11, 16)


def leg(side, right, strike, mid, expiry="2026-11-20"):
    return {"side": side, "right": right, "strike": strike, "mid": mid, "expiry": expiry}


# ------------------------------------------------------------------ payoff
def test_payoff_long_call_and_spreads():
    p = payoff([leg("buy", "call", 100, 2.5)], 100)
    assert p["max_loss"] == -250 and p["unlimited_profit"] and p["breakevens"] == [102.5] and p["capital"] == 250
    p = payoff([leg("buy", "call", 100, 3.0), leg("sell", "call", 105, 1.0)], 100)       # bull call spread
    assert (p["max_profit"], p["max_loss"], p["breakevens"], p["capital"]) == (300, -200, [102.0], 200)
    p = payoff([leg("sell", "put", 95, 1.5), leg("buy", "put", 90, 0.5)], 100)            # bull put spread
    assert (p["max_profit"], p["max_loss"], p["breakevens"], p["net_premium"]) == (100, -400, [94.0], -100)
    assert p["capital"] == 400
    p = payoff([leg("sell", "put", 95, 2.0)], 100)                                        # cash-secured put
    assert p["capital"] == 9500 and p["max_loss"] == -9300 and "现金担保" in p["capital_note"]


def test_payoff_straddle_condor_naked():
    p = payoff([leg("buy", "call", 100, 3.0), leg("buy", "put", 100, 2.0)], 100)
    assert p["breakevens"] == [95.0, 105.0] and p["max_loss"] == -500 and p["unlimited_profit"]
    ic = [leg("buy", "put", 90, 0.4), leg("sell", "put", 95, 1.2), leg("sell", "call", 105, 1.1), leg("buy", "call", 110, 0.3)]
    p = payoff(ic, 100)
    assert p["max_profit"] == 160 and p["max_loss"] == -340 and p["breakevens"] == [93.4, 106.6]
    p = payoff([leg("sell", "call", 105, 1.0)], 100)
    assert p["unlimited_loss"] and p["max_loss"] is None and p["capital"] > 0 and "Reg T" in p["capital_note"]


# ------------------------------------------------------------- the 4 rules
def chain():
    def c(right, strike, bid, ask, oi):
        return {"code": f"US.X261120{right[0].upper()}{int(strike * 1000)}", "right": right, "strike": strike,
                "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4), "iv": 40.0, "oi": oi, "volume": 100}
    return {"expirations": [
        {"expiry": "2026-11-20", "dte": 4, "contracts": [c("call", 100, 2.9, 3.1, 5000), c("call", 105, 0.95, 1.05, 3000),
                                                          c("call", 110, 0.2, 0.3, 4000), c("put", 95, 1.0, 1.1, 300)]}]}


def opt_plan(legs, **kw):
    return {"type": "option", "structure": "test", "legs": legs, **kw}


def test_rules_remove_bad_plans_and_recompute():
    good = opt_plan([{"side": "buy", "right": "call", "strike": 100, "expiry": "2026-11-20", "mid": 99, "bid": 1},
                     {"side": "sell", "right": "call", "strike": 105, "expiry": "2026-11-20"}])
    far = opt_plan([{"side": "buy", "right": "call", "strike": 100, "expiry": "2026-11-27"}])
    missing = opt_plan([{"side": "buy", "right": "call", "strike": 101, "expiry": "2026-11-20"}])
    illiquid = opt_plan([{"side": "sell", "right": "put", "strike": 95, "expiry": "2026-11-20"}])
    wide = opt_plan([{"side": "buy", "right": "call", "strike": 110, "expiry": "2026-11-20"}])
    kept, removed = check_trade_plans([good, far, missing], "买入", chain(), TODAY, 100)
    assert len(kept) == 1 and len(removed) == 2
    assert "超过 7 天" in removed[0]["reasons"][0] and "期权链中没有" in removed[1]["reasons"][0]
    legs = kept[0]["legs"]
    assert legs[0]["mid"] == 3.0 and legs[0]["bid"] == 2.9                # chain values, not the model's 99 / 1
    assert kept[0]["computed"]["max_loss"] == -200 and kept[0]["computed"]["breakevens"] == [102.0]
    kept, removed = check_trade_plans([illiquid, wide], "买入", chain(), TODAY, 100)
    assert not kept and "低于 500 张" in removed[0]["reasons"][0] and "超过 10%" in removed[1]["reasons"][0]
    kept, removed = check_trade_plans([good], "观望", chain(), TODAY, 100)
    assert kept == [] and "观望" in removed[0]["reasons"][0]


def test_stock_plan():
    p = {"type": "stock", "structure": "正股", "stock_entry": {"low": 99, "high": 101, "stop": 95, "target": 110, "holding_days": 5}}
    kept, removed = check_trade_plans([p], "买入", chain(), TODAY, 100)
    assert kept[0]["computed"]["max_loss_per_share"] == 6 and kept[0]["computed"]["reward_per_share"] == 10
    bad = {"type": "stock", "stock_entry": {"low": 99, "high": 101, "stop": 102, "target": 110}}
    assert check_trade_plans([bad], "买入", chain(), TODAY, 100)[0] == []


# ---------------------------------------------------------------- parsing
def test_extract_json_and_normalize():
    assert extract_json('```json\n{"conclusion": {"action": "观望"}}\n```')["conclusion"]["action"] == "观望"
    assert extract_json('说明文字 {"a": 1} 然后 {"conclusion": {"action": "买入"}, "news": []} 结束')["conclusion"]
    n = normalize({"conclusion": {"action": "buy?"}, "news": [{"title": "x", "status": "CONFIRMED", "category": "Earnings",
                                                               "sources": [{"name": "a", "url": "javascript:x"}]}]})
    assert n["conclusion"]["action"] == "观望" and n["news"][0]["status"] == "confirmed"
    assert n["news"][0]["category"] == "earnings" and n["news"][0]["sources"][0]["url"] == ""


def test_parse_json_split_by_citations():
    blocks = [SimpleNamespace(type="text", text="我先搜索。"), SimpleNamespace(type="server_tool_use"),
              SimpleNamespace(type="web_search_tool_result"),
              SimpleNamespace(type="text", text='{"conclusion": {"action": "买入", "reasons": ["收入增长'),
              SimpleNamespace(type="text", text='（见来源）"]}, "news": []}')]
    data, problems = AnalysisLLM._parse(blocks)
    assert not problems and data["conclusion"]["reasons"] == ["收入增长（见来源）"]


def test_env_loader(tmp_path, monkeypatch):
    monkeypatch.delenv("DASH_TEST_KEY", raising=False)
    monkeypatch.setenv("DASH_TEST_KEEP", "env")
    f = tmp_path / ".env"
    f.write_text('# c\nDASH_TEST_KEY="abc"\nexport DASH_TEST_KEEP=file\nBAD LINE\n', encoding="utf-8")
    assert load_env(f) == ["DASH_TEST_KEY"]
    import os
    assert os.environ["DASH_TEST_KEY"] == "abc" and os.environ["DASH_TEST_KEEP"] == "env"
    monkeypatch.delenv("DASH_TEST_KEY")


# ------------------------------------------------------------- API (mock)
def wait(pred, timeout=20.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


@pytest.fixture()
def client(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfg)
    store = ConfigStore(cfg)
    store.set(("stocks", "watchlist"), [{"code": "US.QQQ", "subtypes": ["QUOTE"]}], save=False)
    store.set(("options", "underlyings"), [], save=False)
    store.set(("market", "plate_set"), "INDUSTRY", save=False)
    store.set(("market", "exclude_codes"), ["US.BABA"], save=False)
    store.set(("stock_analysis", "enabled"), True, save=False)
    store.set(("rate_limits", "get_plate_stock"), {"max_calls": 300, "window_sec": 30}, save=False)
    store.save()
    app = create_app(cfg, True, ROOT, 8600, extra_hosts={"testserver"}, data_root=tmp_path)
    with TestClient(app) as c:
        wait(lambda: c.get("/api/state").json()["status"]["connection"] == "connected")
        c.state = app.state.s
        yield c


def test_disabled_switch(client, tmp_path):
    # shipped config keeps the tab off
    assert ConfigStore(ROOT / "config.yaml").get("stock_analysis", "enabled") is False
    assert client.get("/api/state").json()["analysis"]["enabled"] is True
    client.state.cfg.set(("stock_analysis", "enabled"), False, save=False)
    assert client.get("/api/stock-analysis/settings").json() == {"enabled": False}
    assert client.get("/api/state").json()["analysis"] == {"enabled": False}
    r = client.post("/api/stock-analysis", json={"ticker": "NVDA"}, headers=H)
    assert r.status_code == 404 and "已关闭" in r.json()["detail"]
    assert client.get("/api/stock-analysis/job/NVDA").status_code == 404
    assert client.get("/api/stock-analysis/search?q=NV").status_code == 404
    assert not (tmp_path / "data" / "mock" / "analysis" / "US_NVDA.json").exists()
    client.state.cfg.set(("stock_analysis", "enabled"), True, save=False)
    assert client.get("/api/stock-analysis/search?q=NV").status_code == 200


def test_mock_analysis_flow(client):
    r = client.post("/api/stock-analysis", json={"ticker": "nvda"}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ticker"] == "NVDA" and d["mode"] == "mock" and d["cached"] is False
    assert [s["state"] for s in d["progress"]["steps"]] == ["done"] * 5
    a = d["analysis"]
    assert a["conclusion"]["action"] in ("买入", "卖出") and len(a["valuation"]["methods"]) >= 3
    today = now_et().date()
    assert d["validation"]["latest_allowed_expiry"]
    for p in a["trade_plans"]:
        for lg in p.get("legs", []):
            assert 0 <= (dt.date.fromisoformat(lg["expiry"]) - today).days <= 7 and lg["code"].startswith("US.NVDA")
            assert lg["open_interest"] >= 500 and (lg["ask"] - lg["bid"]) <= 0.1 * lg["mid"] + 1e-9
        assert p["computed"]
    r2 = client.post("/api/stock-analysis", json={"ticker": "NVDA"}, headers=H).json()
    assert r2["cached"] is True and r2["generated_at"] == d["generated_at"]
    r3 = client.post("/api/stock-analysis", json={"ticker": "NVDA", "refresh": True}, headers=H).json()
    assert r3["cached"] is False
    assert client.get("/api/stock-analysis/job/US.NVDA").json()["status"] == "done"
    assert "ANTHROPIC" not in r.text and "sk-ant" not in r.text


def test_search_and_us_only(client):
    c = client.get("/api/stock-analysis/search", params={"q": "nvd"}).json()["candidates"]
    assert c and c[0]["code"] == "US.NVDA"
    assert client.post("/api/stock-analysis", json={"ticker": "HK.00700"}, headers=H).status_code == 400
    assert client.post("/api/stock-analysis", json={"ticker": "US.VIX"}, headers=H).status_code == 400
    r = client.post("/api/stock-analysis", json={"ticker": "BABA"}, headers=H)
    assert r.status_code == 400 and "中概股" in r.json()["detail"]
    assert all(x["code"] != "US.BABA" for x in client.get("/api/stock-analysis/search", params={"q": "BAB"}).json()["candidates"])


def test_live_path_with_fake_llm(client, monkeypatch):
    """Live branch with a stand-in for the model: bad related companies and bad plans are removed in code."""
    svc = client.state.analysis
    svc.simulate = False
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = client.post("/api/stock-analysis", json={"ticker": "AMD"}, headers=H)
    assert r.status_code == 400 and ".env" in r.json()["detail"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    seen = {}

    class FakeLLM:
        async def run(self, payload, on_event):
            seen["payload"] = payload
            on_event("search")
            on_event("text")
            e = payload["option_chain"]["expirations"][0]
            liquid = [c for c in e["contracts"] if c["oi"] and c["oi"] >= 500 and c["mid"]
                      and (c["ask"] - c["bid"]) <= 0.1 * c["mid"]]
            c0 = liquid[0]
            plans = [opt_plan([{"side": "buy", "right": c0["right"], "strike": c0["strike"], "expiry": e["expiry"]}]),
                     opt_plan([{"side": "buy", "right": "call", "strike": 1.0, "expiry": e["expiry"]}])]
            data = {"conclusion": {"action": "买入", "direction_7d": "偏多", "reasons": ["r"], "top_risk": "t"},
                    "fundamentals": {"earnings_quality_grade": "C", "metrics": []},
                    "related_companies": [{"ticker": "NVDA", "name": "Nvidia", "relation": "competitor"},
                                          {"ticker": "BABA", "name": "Alibaba", "relation": "customer"},
                                          {"ticker": "0700.HK", "name": "Tencent", "relation": "customer"}],
                    "news": [{"title": "AMD news", "sources": [{"name": "a", "url": "https://example.com/a"}]}],
                    "trade_plans": plans}
            return {"data": data, "searched_urls": {"https://example.com/a"}, "searches": 1, "usage": {},
                    "model": "fake", "attempts": 1}

    svc.llm = FakeLLM()
    d = client.post("/api/stock-analysis", json={"ticker": "AMD"}, headers=H).json()
    p = seen["payload"]
    assert p["ticker"] == "AMD" and "option_chain" in p and "code" not in p["option_chain"]["expirations"][0]["contracts"][0]
    assert [r["ticker"] for r in d["analysis"]["related_companies"]] == ["NVDA"]
    assert len(d["filtered"]["related_removed"]) == 2
    assert len(d["analysis"]["trade_plans"]) == 1 and len(d["validation"]["removed"]) == 1
    assert d["analysis"]["news"][0]["sources"][0]["in_search"] is True
    assert d["validation"]["warnings"]                                     # grade C with 买入
