"""End-to-end check of the backend in mock mode (no OpenD needed).

Covers the acceptance flow that can run without a market: watchlist + subscription
rules, index rejection, option chain + both quote modes, alerts, the master switch,
the two-step live confirmation, and one paper rule end to end
(trigger -> limit order at mid -> fill -> stop -> emergency stop -> complete log).
"""
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import ConfigStore

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Dashboard": "1"}


def wait(pred, timeout=15.0, step=0.2):
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
    # known starting state, independent of what the user has in their own config.yaml
    store = ConfigStore(cfg)
    store.set(("stocks", "watchlist"), [{"code": "US.QQQ", "subtypes": ["QUOTE", "K_5M"]}], save=False)
    store.set(("options", "underlyings"), [], save=False)
    store.set(("options", "pinned"), [], save=False)
    store.set(("auto_trading", "real_acc_id"), None, save=False)
    store.set(("news", "mock_interval_sec"), 2, save=False)
    store.set(("market", "plate_set"), "INDUSTRY", save=False)
    store.set(("rate_limits", "get_plate_stock"), {"max_calls": 200, "window_sec": 30}, save=False)  # faster mock load
    store.save()
    app = create_app(cfg, True, ROOT, 8600, extra_hosts={"testserver"}, data_root=tmp_path)
    with TestClient(app) as c:
        wait(lambda: c.get("/api/state").json()["status"]["connection"] == "connected")
        c.cfg_path = cfg
        c.tmp = tmp_path
        yield c


def test_security_rules(client):
    assert client.post("/api/stocks", json={"code": "US.NVDA"}).status_code == 403           # no header
    r = client.post("/api/stocks", json={"code": "US.NVDA"}, headers={**H, "Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/state", headers={"Host": "evil.example:8600"}).status_code == 403


def test_watchlist_and_subscriptions(client):
    r = client.post("/api/stocks", json={"code": "nvda"}, headers=H)
    assert r.status_code == 200 and r.json()["code"] == "US.NVDA"
    for bad, word in (("US..IXIC", "ETF"), ("US.VIX", "VIXY"), ("US.NDX", "QQQ"), ("HK.00700", "仅支持美股")):
        r = client.post("/api/stocks", json={"code": bad}, headers=H)
        assert r.status_code == 400 and word in r.json()["detail"], (bad, r.json())
    r = client.post("/api/stocks/US.NVDA/subtypes", json={"subtype": "QUOTE", "enabled": True}, headers=H)
    assert r.status_code == 200 and "QUOTE" in r.json()["subtypes"]
    r = client.post("/api/stocks/US.NVDA/subtypes", json={"subtype": "QUOTE", "enabled": False}, headers=H)
    msg = " ".join(r.json()["messages"])
    assert "至少 1 分钟" in msg and "自动退订" in msg
    subs = client.get("/api/state").json()["subs"]
    assert subs["US.NVDA"]["QUOTE"]["unsub_in"] > 0                                        # scheduled, not dropped
    q = client.get("/api/state").json()["status"]["quota"]
    assert q["stock_total"] == 300 and q["option_total"] == 60
    assert "US.NVDA" in client.cfg_path.read_text(encoding="utf-8")                        # persisted


def test_options_chain_and_modes(client):
    r = client.post("/api/options/underlyings", json={"code": "US.QQQ"}, headers=H)
    assert r.status_code == 200
    exp = r.json()["expirations"][0]["date"]
    meta = client.post("/api/options/expirations", json={"underlying": "US.QQQ", "date": exp}, headers=H).json()
    assert meta["strikes"] and meta["mode"] == "snapshot"
    bad = client.patch(f"/api/options/expirations/US.QQQ/{exp}", json={"interval_sec": 0.5}, headers=H)
    assert bad.status_code == 400 and "1 秒" in bad.json()["detail"]
    r = client.patch(f"/api/options/expirations/US.QQQ/{exp}", json={"mode": "subscribe"}, headers=H)
    assert r.status_code == 200, r.json()
    assert client.get("/api/state").json()["status"]["quota"]["option_used"] == 2          # QUOTE + ORDER_BOOK
    call = meta["strikes"][len(meta["strikes"]) // 2]["call"]
    assert client.post("/api/options/pinned", json={"code": call}, headers=H).status_code == 200
    wait(lambda: (client.get("/api/state").json()["quotes"].get(call) or {}).get("mid"))
    # chain is cached on disk for the day
    assert list((client.tmp / "data" / "mock" / "cache" / "chains").rglob("*.json"))


def test_rule_order_link_requires_switch_and_live_needs_two_steps(client):
    rule = {"name": "t", "symbol": "US.QQQ", "conditions": [{"type": "price_above", "value": 1}],
            "order_action": {"enabled": True, "type": "single", "side": "BUY",
                             "contract": "US.QQQ261113C675000", "quantity": 1}}
    r = client.post("/api/alerts", json=rule, headers=H)
    assert r.status_code == 400 and "总开关" in r.json()["detail"]
    r = client.post("/api/trading/env", json={"env": "REAL"}, headers=H)
    assert r.status_code == 400 and "实盘账户" in r.json()["detail"]
    real = client.get("/api/trading").json()["real_accounts"][0]["acc_id"]
    assert client.post("/api/trading/real_account", json={"acc_id": real}, headers=H).status_code == 200
    step1 = client.post("/api/trading/env", json={"env": "REAL"}, headers=H).json()
    assert step1["done"] is False and step1["challenge"]
    wrong = client.post("/api/trading/env/confirm", json={"challenge": step1["challenge"], "phrase": "yes"}, headers=H)
    assert wrong.status_code == 400
    ok = client.post("/api/trading/env/confirm", json={"challenge": step1["challenge"], "phrase": step1["phrase"]},
                     headers=H).json()
    assert ok["env"] == "REAL"
    t = client.get("/api/trading").json()
    assert t["env"] == "REAL" and t["auto_enabled"] is False
    assert client.post("/api/trading/env", json={"env": "SIMULATE"}, headers=H).json()["env"] == "SIMULATE"
    r = client.put("/api/trading/limits", json={"limits": {"max_orders_per_day": 3}}, headers=H)
    assert r.status_code == 400                                                              # needs confirmation


def test_paper_rule_end_to_end(client):
    # pick a liquid-looking contract from the mock chain
    exp = client.post("/api/options/underlyings", json={"code": "US.QQQ"}, headers=H).json()["expirations"][-1]["date"]
    meta = client.post("/api/options/expirations", json={"underlying": "US.QQQ", "date": exp}, headers=H).json()
    contract = meta["strikes"][len(meta["strikes"]) // 2]["call"]
    client.post("/api/options/pinned", json={"code": contract}, headers=H)
    # tight stop so the position closes quickly in the simulation
    r = client.put("/api/trading/limits", json={"limits": {"stop_loss_pct": 1, "take_profit_pct": 1,
                                                           "time_stop_et": "23:59"}, "confirmed": True}, headers=H)
    assert r.status_code == 200
    assert client.post("/api/trading/auto", json={"enabled": True}, headers=H).json()["auto_enabled"] is True
    rule = {"name": "e2e", "symbol": "US.QQQ", "conditions": [{"type": "price_above", "value": 1}],
            "trigger_once": True,
            "order_action": {"enabled": True, "type": "single", "side": "BUY", "contract": contract, "quantity": 1}}
    r = client.post("/api/alerts", json=rule, headers=H)
    assert r.status_code == 200, r.json()

    def log_events():
        return [e["event"] for e in client.get("/api/trading/log").json()["entries"]]

    wait(lambda: "place" in log_events(), timeout=20)
    wait(lambda: "fill" in log_events(), timeout=20)
    wait(lambda: any(p["status"] in ("open", "exit_pending", "closed")
                     for p in client.get("/api/trading").json()["positions"]), timeout=20)
    # nudge the underlying so the 1 % stop / target triggers
    client.post("/api/mock/set_price", json={"code": "US.QQQ", "price": 640}, headers=H)
    wait(lambda: any(e["event"] == "place" and e.get("role") == "exit"
                     for e in client.get("/api/trading/log").json()["entries"]), timeout=25)
    r = client.post("/api/trading/emergency_stop", headers=H)
    assert r.status_code == 200
    t = client.get("/api/trading").json()
    assert t["auto_enabled"] is False
    entries = client.get("/api/trading/log").json()["entries"]
    events = [e["event"] for e in entries]
    for needed in ("auto_switch", "place", "order_update", "fill", "emergency_stop"):
        assert needed in events, (needed, events)
    entry = next(e for e in entries if e["event"] == "place" and e.get("role") == "entry")
    for field in ("ts", "contract", "side", "qty", "price", "rule", "raw"):
        assert entry.get(field) not in (None, ""), field
    assert entry["quote"]["mid"] > 0
    # order log is a real JSONL file
    files = list((client.tmp / "logs" / "mock" / "orders").glob("orders-*.jsonl"))
    assert files and all(json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines())


def test_quote_right_loss_and_reconnect_restore(client):
    client.post("/api/stocks", json={"code": "US.AAPL"}, headers=H)
    client.post("/api/stocks/US.AAPL/subtypes", json={"subtype": "K_5M", "enabled": True}, headers=H)
    # seeded history gives the most recent *completed* 5 minute close right away (time_key = bar end, ET)
    q = wait(lambda: (client.get("/api/state").json()["quotes"].get("US.AAPL") or {}).get("k5_close") and
             client.get("/api/state").json()["quotes"]["US.AAPL"])
    assert q["k5_time"] < q.get("k5_forming_time", "9999") or q.get("k5_forming") is not None
    client.post("/api/mock/lose_quote_right", headers=H)
    wait(lambda: client.get("/api/state").json()["status"]["right_lost"], timeout=10)
    r = client.post("/api/status/quote_right", headers=H).json()
    assert r["ok"] and r["right_lost"] is False
    client.post("/api/mock/disconnect", json={"seconds": 2}, headers=H)
    wait(lambda: client.get("/api/state").json()["status"]["connection"] == "disconnected", timeout=5)
    wait(lambda: client.get("/api/state").json()["status"]["connection"] == "connected", timeout=10)
    wait(lambda: (client.get("/api/state").json()["status"]["quota"]["stock_used"] or 0) >= 1, timeout=10)


def test_market_page_mock(client):
    def ready():
        s = client.get("/api/market").json()
        return s if s["gainers"] and s["leaders"] and s["inflow"] else None

    st = wait(ready, timeout=45)
    assert st["change_method"] == "plate_snapshot" and st["flow_method"] == "constituents"
    assert all(l["market_cap"] >= 2e9 and l["dollar_volume"] >= 2e7 for l in st["leaders"])   # size filter
    assert [b["code"] for b in st["benchmarks"]] == ["US.SPY", "US.QQQ", "US.TQQQ", "US.DIA", "US.SOXX"]
    ch = [g["change_pct"] for g in st["gainers"]]
    assert ch == sorted(ch, reverse=True)
    assert st["budget"]["get_market_snapshot"]["cap"] == 30
    assert st["budget"]["get_market_snapshot"]["used"] <= 30
    # US stocks only: China-themed plates hidden, China-concept stocks removed everywhere
    china = {"US.BABA", "US.PDD", "US.NIO", "US.BIDU", "US.JD", "US.LI", "US.XPEV", "US.TCEHY", "US.BILI", "US.NTES", "US.HUYA"}
    shown = [r["name"] for k in ("gainers", "losers", "inflow", "outflow") for r in st[k]] + [l["sector"] for l in st["leaders"]]
    assert "热门中概股" not in shown and "腾讯概念" not in shown
    assert not {l["code"] for l in st["leaders"]} & china
    assert st["us_only"]["enabled"]
    det = client.get(f"/api/market/sector/{st['gainers'][0]['code']}").json()
    pcts = [c["change_pct"] for c in det["constituents"]]
    assert pcts == sorted(pcts, reverse=True)
    assert client.post("/api/market/plate_set", json={"plate_set": "BAD"}, headers=H).status_code == 400
    assert client.post("/api/market/plate_set", json={"plate_set": "CONCEPT"}, headers=H).json()["plate_set"] == "CONCEPT"
    r = client.put("/api/market/size_filter", json={"min_market_cap_usd": 0, "min_dollar_volume_usd": 0}, headers=H)
    assert r.status_code == 200
    bad = client.put("/api/market/benchmarks", json={"codes": ["US..IXIC"]}, headers=H)
    assert bad.status_code == 400 and "ETF" in bad.json()["detail"]
    # the page never subscribes
    assert client.get("/api/state").json()["status"]["quota"]["stock_used"] == 2   # only the QQQ watchlist subs


def test_news_mock_and_rules(client):
    evs = wait(lambda: client.get("/api/news").json()["events"], timeout=30)
    e = evs[0]
    for f in ("time_et", "sources", "title", "level", "symbols", "links", "count"):
        assert f in e
    wait(lambda: any(x["count"] > 1 for x in client.get("/api/news?hours=1").json()["events"]), timeout=40)  # dedup
    only_high = client.get("/api/news?levels=高").json()["events"]
    assert all(x["level"] == "高" for x in only_high)
    client.post("/api/stocks", json={"code": "US.NVDA"}, headers=H)          # earnings filter follows the watchlist
    rows = wait(lambda: client.get("/api/news").json()["earnings"], timeout=25)
    assert all(r["code"] in ("US.QQQ", "US.NVDA") and r["pub_zh"] in ("盘前", "盘后", "盘中") for r in rows)
    # settings
    assert client.put("/api/news/settings", json={"contact_email": "not-an-email"}, headers=H).status_code == 400
    s = client.put("/api/news/settings", json={"contact_email": "dev@example.com", "notify_medium": True,
                                               "keywords": {"high": ["stock split"], "medium": ["conference"]}},
                   headers=H).json()
    assert s["contact_email"] == "dev@example.com" and s["notify_medium"] and s["keywords"]["high"] == ["stock split"]
    assert s["layer2"]["enabled"] is False and s["layer2"]["daily_cap"] == 300
    names = {x["name"] for x in client.get("/api/state").json()["status"]["news_sources"]}
    assert {"moomoo_search", "moomoo_api", "sec", "earnings", "layer2", "mock"} <= names
    # news-only rules may not carry an order action
    client.post("/api/trading/auto", json={"enabled": True}, headers=H)
    rule = {"name": "n", "symbol": "US.QQQ", "conditions": [{"type": "news_high_within", "value": 30}],
            "order_action": {"enabled": True, "type": "single", "side": "BUY", "contract": "US.QQQ261113C675000",
                             "quantity": 1}}
    r = client.post("/api/alerts", json=rule, headers=H)
    assert r.status_code == 400 and "新闻条件" in r.json()["detail"]
    rule["conditions"].append({"type": "price_above", "value": 1})
    rule["order_action"]["enabled"] = False
    assert client.post("/api/alerts", json=rule, headers=H).status_code == 200



def test_market_us_only_recomputes_sectors_with_china_stocks(client):
    def ready():
        s = client.get("/api/market").json()
        return s if s["us_only"]["recomputed"] and s["leaders"] else None
    st = wait(ready, timeout=45)
    assert st["us_only"]["china_codes"] >= 11
    concept = client.post("/api/market/plate_set", json={"plate_set": "CONCEPT"}, headers=H).json()
    assert {"热门中概股", "腾讯概念"} <= set(concept["us_only"]["hidden_plates"])        # China-themed plates hidden
    client.post("/api/market/plate_set", json={"plate_set": "INDUSTRY"}, headers=H)
    ev_code = next(p["code"] for p in client.get("/api/market").json()["gainers"] + st["losers"]
                   if p["name"] in ("电动汽车", "互联网服务")) if any(
        p["name"] in ("电动汽车", "互联网服务") for p in st["gainers"] + st["losers"]) else None
    if ev_code:
        det = client.get(f"/api/market/sector/{ev_code}").json()
        assert not {c["code"] for c in det["constituents"]} & {"US.NIO", "US.BABA", "US.BIDU"}
