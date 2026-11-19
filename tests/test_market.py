"""Weighted sector change, leader size filter, budget split in the rate limiter, news alert condition."""
import asyncio
import datetime as dt
import time

import pytest

from backend.alerts import NO_NEWS_AGE, AlertEngine, RuleError, parse_rule
from backend.market.calc import passes_size_filter, pct_change, sector_leader, top_bottom, up_down_counts, weighted_change
from backend.ratelimit import Limit, Priority, SlidingWindowLimiter
from backend.timeutil import ET

TOL = 0.03


# ----------------------------------------------------------- weighted change
def test_weighted_change_uses_market_cap():
    cons = [{"change_pct": 2.0, "market_cap": 300e9}, {"change_pct": -4.0, "market_cap": 100e9}]
    assert weighted_change(cons) == pytest.approx(0.5)          # (2*300 - 4*100) / 400


def test_weighted_change_skips_unusable_rows():
    cons = [{"change_pct": 1.0, "market_cap": 10e9}, {"change_pct": None, "market_cap": 50e9},
            {"change_pct": 9.0, "market_cap": None}, {"change_pct": 5.0, "market_cap": 0}]
    assert weighted_change(cons) == pytest.approx(1.0)
    assert weighted_change([]) is None


def test_pct_change():
    assert pct_change(101, 100) == pytest.approx(1.0)
    assert pct_change(1, 0) is None and pct_change(None, 5) is None


# ----------------------------------------------------------------- leaders
CONS = [
    {"code": "US.MICRO", "change_pct": 40.0, "market_cap": 0.3e9, "dollar_volume": 90e6},    # too small
    {"code": "US.THIN", "change_pct": 25.0, "market_cap": 8e9, "dollar_volume": 5e6},        # too illiquid
    {"code": "US.MID", "change_pct": 6.0, "market_cap": 2.5e9, "dollar_volume": 30e6},
    {"code": "US.BIG", "change_pct": 6.0, "market_cap": 900e9, "dollar_volume": 9e9},        # tie -> bigger volume
    {"code": "US.DOWN", "change_pct": -3.0, "market_cap": 50e9, "dollar_volume": 1e9},
]


def test_size_filter_defaults():
    assert passes_size_filter(CONS[2], 2e9, 20e6)
    assert not passes_size_filter(CONS[0], 2e9, 20e6)
    assert not passes_size_filter(CONS[1], 2e9, 20e6)
    assert not passes_size_filter({"market_cap": None, "dollar_volume": 1e9}, 2e9, 20e6)


def test_sector_leader_ignores_micro_and_illiquid_caps():
    assert sector_leader(CONS, 2e9, 20e6)["code"] == "US.BIG"
    assert sector_leader(CONS, 0, 0)["code"] == "US.MICRO"          # filter off
    assert sector_leader(CONS[:2], 2e9, 20e6) is None


def test_up_down_counts_and_top_bottom():
    assert up_down_counts(CONS) == (4, 1, 0)
    rows = [{"n": i, "v": v} for i, v in enumerate([5, -1, 3, -7, 0.5])]
    top, bottom = top_bottom(rows, "v", 2)
    assert [r["v"] for r in top] == [5, 3] and [r["v"] for r in bottom] == [-7, -1]
    top, bottom = top_bottom(rows, "v", 10)
    assert all(r["v"] > 0 for r in top) and all(r["v"] < 0 for r in bottom)   # a rising sector is never a "loser"
    top, bottom = top_bottom([{"v": 1.0}, {"v": 2.0}], "v", 10)
    assert bottom == []                                          # broad rally: no losers listed


# ------------------------------------------------------------- budget split
def test_consumer_cap_limits_market_page_to_half_budget():
    async def main():
        lim = SlidingWindowLimiter({"snap": Limit(6, 0.6, consumer_caps={"market": 3})})
        stamps = []

        async def market():
            await lim.acquire("snap", Priority.BACKGROUND, "market")
            stamps.append(time.monotonic())

        t0 = time.monotonic()
        await asyncio.gather(*(market() for _ in range(6)))
        return t0, sorted(stamps), lim.usage()["snap"]["consumers"]["market"]

    t0, s, usage = asyncio.run(main())
    assert s[2] - t0 < 0.1                     # first 3 immediately
    assert s[3] - s[0] >= 0.6 - TOL            # 4th waits for the window although the bucket has room
    assert usage["cap"] == 3


def test_capped_consumer_does_not_block_other_requests():
    async def main():
        lim = SlidingWindowLimiter({"snap": Limit(10, 0.8, consumer_caps={"market": 2})})
        order = []

        async def req(name, prio, consumer=None):
            await lim.acquire("snap", prio, consumer)
            order.append((name, time.monotonic()))

        t0 = time.monotonic()
        # market fills its cap, then queues; a stock-page poll and an order arrive later
        tasks = [asyncio.create_task(req(f"m{i}", Priority.NORMAL, "market")) for i in range(4)]
        await asyncio.sleep(0.05)
        tasks.append(asyncio.create_task(req("stocks", Priority.POLL)))
        tasks.append(asyncio.create_task(req("order", Priority.ORDER)))
        await asyncio.gather(*tasks)
        return t0, dict(order)

    t0, done = asyncio.run(main())
    assert done["stocks"] - t0 < 0.2 and done["order"] - t0 < 0.2     # not stuck behind the capped consumer
    assert done["m2"] - t0 >= 0.8 - TOL                               # market waited for its own window


def test_market_page_never_eats_order_reserve():
    async def main():
        lim = SlidingWindowLimiter({"snap": Limit(4, 0.6, reserve_high=1, consumer_caps={"market": 10})})
        for _ in range(3):
            await lim.acquire("snap", Priority.BACKGROUND, "market")
        t0 = time.monotonic()
        await lim.acquire("snap", Priority.ORDER)       # the reserved slot is still free for the order
        return time.monotonic() - t0

    assert asyncio.run(main()) < 0.05


# ----------------------------------------------------------- news condition
def et(h, m):
    return dt.datetime(2026, 11, 11, h, m, tzinfo=ET)


def test_news_condition_combines_with_price():
    rule = parse_rule({"id": "n1", "symbol": "US.NVDA", "conditions": [
        {"type": "news_high_within", "value": 30}, {"type": "price_below", "value": 180}]})
    assert not rule.news_only()
    e = AlertEngine()
    e.set_rules([rule])
    assert e.evaluate({"US.NVDA": {"last": 170, "news_high_age_min": NO_NEWS_AGE}}, et(10, 0), 1) == []
    assert e.status("n1", {"US.NVDA": {"last": 170, "news_high_age_min": NO_NEWS_AGE}}, et(10, 0), 1) == "watching"
    assert e.evaluate({"US.NVDA": {"last": 170, "news_high_age_min": 45}}, et(10, 0), 2) == []
    assert len(e.evaluate({"US.NVDA": {"last": 170, "news_high_age_min": 12}}, et(10, 0), 3)) == 1


def test_news_only_rule_detection_and_validation():
    assert parse_rule({"symbol": "US.NVDA", "conditions": [{"type": "news_high_within", "value": 60}]}).news_only()
    with pytest.raises(RuleError):
        parse_rule({"symbol": "US.NVDA", "conditions": [{"type": "news_high_within", "value": 0}]})


# --------------------------------------------- refresh loop must always yield
class _FakeCfg:
    def __init__(self, data):
        self.data = data

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


class _FakeApp:
    def __init__(self):
        self.cfg = _FakeCfg({"market": {"plate_set": "CONCEPT", "refresh_sec": 60}})
        self.published = 0
        app = self

        class GW:
            @staticmethod
            def connection_state():
                return "connected"

        class Hub:
            @staticmethod
            def publish(kind, data):
                app.published += 1

        class Lim:
            @staticmethod
            def usage():
                return {}

        self.gw, self.hub, self.limiter, self.client = GW(), Hub(), Lim(), None


def test_refresh_loop_yields_when_selected_plate_set_has_no_list(tmp_path):
    """Regression: switching to 概念板块 before its plate list loaded made the loop spin without yielding,
    freezing the whole backend and growing memory without bound."""
    import threading
    from backend.market.service import MarketService

    result = {}

    def run():
        async def main():
            svc = MarketService(_FakeApp(), tmp_path)
            svc.plates = {"INDUSTRY": [{"code": "US.LIST1", "name": "x"}]}   # CONCEPT list missing
            svc.updated["sectors"] = "2026-11-11 10:00:00"
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            loop_task = asyncio.create_task(svc._refresh_loop())
            tick_task = asyncio.create_task(ticker())
            await asyncio.sleep(2.6)             # initial 2 s delay + some loop time
            svc._force.set()                     # what set_plate_set does
            await asyncio.sleep(0.6)
            result["ticks"] = ticks
            result["force_cleared"] = not svc._force.is_set()
            loop_task.cancel()
            tick_task.cancel()
        asyncio.run(main())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "market refresh loop starved the event loop"
    assert result["ticks"] > 100 and result["force_cleared"]


# --------------------------------------------------------- US stocks only
def test_china_plate_names():
    from backend.market.calc import is_china_plate
    for name in ("热门中概股", "China's Terrific", "中国概念股", "港股通", "Chinese ADRs", "腾讯概念", "DeepSeek概念股",
                 "内地教育股", "双十一"):
        assert is_china_plate(name), name
    for name in ("油气中游", "半导体", "人工智能", "数据中心 REITs"):
        assert not is_china_plate(name), name


def test_china_codes_and_plate_visibility():
    from backend.market.calc import china_codes, plate_allowed
    plates = [{"code": "P1", "name": "热门中概股"}, {"code": "P2", "name": "互联网零售"}, {"code": "P3", "name": "腾讯概念"}]
    members = {"P1": ["US.BABA", "US.PDD", "US.BILI", "US.NTES"], "P2": ["US.AMZN", "US.EBAY", "US.ETSY", "US.BABA"],
               "P3": ["US.BILI", "US.NTES", "US.SPOT"]}
    cn = china_codes(plates, members, ["US.XYZ"])
    assert cn == {"US.BABA", "US.PDD", "US.BILI", "US.NTES", "US.XYZ"}
    assert not plate_allowed("热门中概股", members["P1"], cn)            # China-themed by name
    assert plate_allowed("互联网零售", members["P2"], cn)               # 1 of 4 China-concept: kept, recomputed
    assert not plate_allowed("腾讯概念", members["P3"], cn)             # mostly China-concept: hidden
    assert not plate_allowed("小板块", ["US.BABA", "US.AAA", "US.BBB", "US.CCC", "US.DDD"][:3] + ["US.PDD"], cn)
    assert plate_allowed("未加载", None, cn)                            # constituents unknown yet: name decides


# --------------------------------------------- one unquotable code must not sink a batch
class _SnapClient:
    def __init__(self, bad, name_it=True):
        self.bad, self.name_it, self.calls = set(bad), name_it, 0

    async def snapshot(self, codes, priority, consumer=None):
        from backend.gateway.base import OpenDError
        self.calls += 1
        hit = [c for c in codes if c in self.bad]
        if hit:
            raise OpenDError(f"暂不提供美股 OTC 市场行情 {hit[0].split('.', 1)[1]}" if self.name_it else "参数错误")
        return [{"code": c, "last_price": 10.0, "prev_close_price": 9.0} for c in codes]


@pytest.mark.parametrize("name_it", [True, False])
def test_snapshot_robust_skips_unquotable_codes(tmp_path, name_it):
    from backend.market.service import MarketService
    app = _FakeApp()
    app.cfg = _FakeCfg({"market": {}, "rate_limits": {"get_market_snapshot": {"max_codes": 4}}})
    app.client = _SnapClient({"US.AAGC", "US.RHHBY"}, name_it)
    svc = MarketService(app, tmp_path)
    codes = [f"US.S{i}" for i in range(9)] + ["US.AAGC", "US.RHHBY"]
    recs = asyncio.run(svc.snapshot_robust(codes, Priority.BACKGROUND))
    assert sorted(r["code"] for r in recs) == sorted(codes[:9])
    assert svc.bad_codes == {"US.AAGC", "US.RHHBY"}
    calls = app.client.calls
    asyncio.run(svc.snapshot_robust(codes, Priority.BACKGROUND))          # remembered: no failed request again
    assert app.client.calls - calls == 3                                   # 9 codes / 4 per request
    assert (tmp_path / "market" / "unsupported_codes.json").exists()


def test_china_set_combines_plates_sec_and_manual_and_keeps_taiwan(tmp_path):
    """China-concept = moomoo China plates + SEC (mainland China / HK / Macau) + exclude_codes;
    Taiwan-based companies that moomoo lists under 热门中概股 (TSM) and keep_codes stay in."""
    from backend.market.service import MarketService
    app = _FakeApp()
    app.cfg = _FakeCfg({"market": {"plate_set": "INDUSTRY", "exclude_codes": ["US.XYZ"], "keep_codes": ["AS"]}})
    svc = MarketService(app, tmp_path)
    svc.plates = {"CONCEPT": [{"code": "P1", "name": "热门中概股"}],
                  "INDUSTRY": [{"code": "P2", "name": "半导体"}, {"code": "P3", "name": "互联网零售"}]}
    svc.members = {"P1": ["US.BABA", "US.TSM", "US.AS"], "P2": ["US.NVDA", "US.TSM", "US.AMD", "US.HSAI"],
                   "P3": ["US.AMZN", "US.EBAY", "US.ETSY", "US.JD"]}
    svc.sec_china, svc.sec_taiwan = {"US.JD", "US.HSAI"}, {"US.TSM"}
    assert svc.china() == {"US.BABA", "US.JD", "US.HSAI", "US.XYZ"}
    assert svc.us_members("P2") == ["US.NVDA", "US.TSM", "US.AMD"]
    assert [p["name"] for p in svc.visible_plates()] == ["半导体", "互联网零售"]
    app.cfg.data["market"]["us_only"] = False
    assert svc.china() == set()


def test_sec_china_list_is_cached_and_skipped_in_mock(tmp_path):
    from backend.market.service import MarketService
    app = _FakeApp()
    app.mock = True
    svc = MarketService(app, tmp_path)
    asyncio.run(svc.ensure_sec_china())          # mock mode: no SEC requests at all
    assert svc.sec_china == set()

    calls = []

    class Sec:
        async def located_tickers(self, ua):
            calls.append(ua)
            return {"china": {"BABA", "BIDU"}, "taiwan": {"TSM"}}

    class News:
        sec = Sec()

        @staticmethod
        def sec_ua():
            return "app someone@example.com"

    app.mock, app.news = False, News()
    asyncio.run(svc.ensure_sec_china())
    asyncio.run(svc.ensure_sec_china())          # same day: cached, no second fetch
    assert calls == ["app someone@example.com"]
    again = MarketService(app, tmp_path)         # survives a restart
    assert again.sec_china == {"US.BABA", "US.BIDU"} and again.sec_taiwan == {"US.TSM"}


def test_sec_china_partial_result_keeps_previous_list(tmp_path):
    from backend.market.service import MarketService
    app = _FakeApp()
    app.mock = False
    svc = MarketService(app, tmp_path)
    svc.sec_china = {"US.BABA", "US.OLD"}

    class Sec:
        async def located_tickers(self, ua):
            return {"china": {"BIDU"}, "taiwan": {"TSM"}, "failed": 2}

    class News:
        sec = Sec()

        @staticmethod
        def sec_ua():
            return "app someone@example.com"

    app.news = News()
    asyncio.run(svc.ensure_sec_china())
    assert svc.sec_china == {"US.BABA", "US.OLD", "US.BIDU"} and svc.sec_taiwan == {"US.TSM"}
    assert svc.sec_china_day is None and "失败" in svc.sec_china_error


# ------------------------------------- capital flow: top 5 per sector, at most 60 codes per round
def _flow_svc(tmp_path, n_plates=30, per_plate=8, extra_cfg=None):
    from backend.market.service import MarketService
    app = _FakeApp()
    app.cfg = _FakeCfg({"market": {"plate_set": "INDUSTRY", "min_market_cap_usd": 1e9, "min_dollar_volume_usd": 1e6,
                                   **(extra_cfg or {})}})
    svc = MarketService(app, tmp_path)
    svc.plates = {"INDUSTRY": [{"code": f"P{i}", "name": f"板块{i}"} for i in range(n_plates)]}
    svc.members, svc.stocks = {}, {}
    for i in range(n_plates):
        codes = [f"US.S{i}_{j}" for j in range(per_plate)]
        svc.members[f"P{i}"] = codes
        for j, c in enumerate(codes):   # j = 0 is the largest by dollar volume; j = 7 fails the size filter
            svc.stocks[c] = {"code": c, "market_cap": 5e9, "dollar_volume": (per_plate - j) * 1e7 if j < 7 else 1e5}
    return svc


def test_flow_targets_are_top5_per_sector_and_at_most_60(tmp_path):
    svc = _flow_svc(tmp_path)
    uni = {s["code"] for s in svc._flow_universe()}
    assert len(uni) == 30 * 5 and all(c.split("_")[1] in "01234" for c in uni)     # only each sector's top 5
    first = svc._flow_targets()
    assert len(first) == 60 and set(first) <= uni
    for c in first:                                                                # measure them (today)
        svc.flows[c] = {"in_flow": 1.0, "time": "2026-11-11 15:59:00", "fetched": time.time()}
    second = svc._flow_targets()
    assert len(second) == 60 and not set(second) & set(first)                      # unmeasured ones first
    assert len(svc._flow_missing()) == 150 - 60


def test_flow_targets_refresh_the_stalest_once_all_are_measured(tmp_path):
    svc = _flow_svc(tmp_path, n_plates=20)
    now = time.time()
    for k, s in enumerate(svc._flow_universe()):
        svc.flows[s["code"]] = {"in_flow": 1.0, "time": "2026-11-11 15:00:00", "fetched": now - k}
    assert svc._flow_missing() == []
    oldest = sorted(svc.flows, key=lambda c: svc.flows[c]["fetched"])[:60]
    assert svc._flow_targets() == oldest


def test_sector_flow_sums_only_top5_and_applies_coverage_threshold(tmp_path):
    svc = _flow_svc(tmp_path, n_plates=2, extra_cfg={"min_flow_coverage_pct": 20})
    for s in svc.stocks.values():
        svc.flows[s["code"]] = {"in_flow": 1e6, "time": "2026-11-11 15:59:00", "fetched": time.time()}
    rows = {r["code"]: r for r in svc._sector_flows()}
    assert rows["P0"]["flow"] == 5e6 and rows["P0"]["covered"] == 5 and rows["P0"]["top"] == 5
    assert rows["P0"]["eligible"] == 7
    # top 5 = (8+7+6+5+4) of (8+...+2) = 30 / 35 of the eligible dollar volume
    assert rows["P0"]["coverage"] == round(30 / 35 * 100, 1)
    # a value from an earlier trading day does not count once today's values exist
    svc.flows["US.S1_0"]["time"] = "2026-11-10 15:59:00"
    r1 = {r["code"]: r for r in svc._sector_flows()}["P1"]
    assert r1["covered"] == 4 and r1["flow"] == 4e6
    # below the coverage threshold: not listed, counted
    svc.app.cfg.data["market"]["min_flow_coverage_pct"] = 90
    assert svc._sector_flows() == [] and svc._flow_below == 2


def test_flow_loop_catches_up_back_to_back_outside_regular_hours(tmp_path, monkeypatch):
    """Regression: outside regular hours only the forced first round ran; the catch-up rounds were skipped."""
    import types
    import backend.market.service as ms
    svc = _flow_svc(tmp_path, n_plates=30)          # 150 top-5 codes -> 3 rounds of 60
    svc.updated["flows"] = "2026-11-10 16:00:00"
    svc._flow_force = True                            # what start() does
    calls = []

    class Client:
        async def capital_flow(self, code, period, prio, consumer):
            calls.append(code)
            return [{"in_flow": 1.0, "capital_flow_item_time": "2026-11-10 15:59:00"}]

    svc.app.client = Client()
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 0.5
        return clock["t"]

    real_sleep = asyncio.sleep
    monkeypatch.setattr(ms, "us_session", lambda: "CLOSED")
    monkeypatch.setattr(ms, "asyncio", types.SimpleNamespace(sleep=lambda s: real_sleep(0)))
    monkeypatch.setattr(ms, "time", types.SimpleNamespace(monotonic=monotonic, time=time.time))

    async def main():
        task = real_create(svc._flow_loop())
        await real_sleep(1.0)
        task.cancel()

    real_create = asyncio.create_task
    asyncio.run(main())
    # rounds 1-2 measure 120 new codes, round 3 the last 30 plus the 30 stalest; then it waits
    assert len(calls) == 180 and len(set(calls)) == 150
    assert svc.flow_round["missing"] == 0 and svc.flow_round["codes"] == 60


def test_flow_catch_up_continues_when_the_universe_grows_mid_round(tmp_path, monkeypatch):
    """Regression: right after a plate-set switch the constituents were not loaded yet, the universe grew
    during the round, and the 'missing went down' check stopped the catch-up."""
    import types
    import backend.market.service as ms
    svc = _flow_svc(tmp_path, n_plates=30)
    later = {c: s for c, s in svc.stocks.items() if int(c.split("_")[0][4:]) >= 6}   # plates 6..29 load later
    for c in later:
        svc.stocks.pop(c)
    svc.updated["flows"] = "2026-11-10 16:00:00"
    svc._flow_force = True
    calls = []

    class Client:
        async def capital_flow(self, code, period, prio, consumer):
            calls.append(code)
            svc.stocks.update(later)          # the sector refresh finished meanwhile
            return [{"in_flow": 1.0, "capital_flow_item_time": "2026-11-10 15:59:00"}]

    svc.app.client = Client()
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 0.5
        return clock["t"]

    real_sleep, real_create = asyncio.sleep, asyncio.create_task
    monkeypatch.setattr(ms, "us_session", lambda: "CLOSED")
    monkeypatch.setattr(ms, "asyncio", types.SimpleNamespace(sleep=lambda s: real_sleep(0)))
    monkeypatch.setattr(ms, "time", types.SimpleNamespace(monotonic=monotonic, time=time.time))

    async def main():
        task = real_create(svc._flow_loop())
        await real_sleep(1.0)
        task.cancel()

    asyncio.run(main())
    assert len(set(calls)) == 150 and svc.flow_round["missing"] == 0
