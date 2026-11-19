"""Keyword scoring, deduplication, SEC rate limiter and SEC parsing."""
import asyncio
import time

import pytest

from backend.news.dedup import categories, clean_title, find_event, normalize, same_event, similarity, url_key
from backend.news.scoring import KeywordSet, mentions, normalize_form, score_item, sec_level
from backend.news.sources import SecEdgar, SourceError, make_http_limiter, parse_moomoo_date

TOL = 0.03


# ------------------------------------------------------------------- scoring
@pytest.mark.parametrize("title, level", [
    ("DOJ opens probe into Nvidia executive", "高"),
    ("FBI raids supplier offices", "高"),
    ("Company recalls 2 million vehicles", "高"),            # plural form of "recall"
    ("Commerce Department tightens export controls on chips", "高"),
    ("英伟达高管遭司法部调查", "高"),
    ("公司宣布出口管制影响", "高"),
    ("Analysts lift price target after earnings", "中"),
    ("公司宣布回购计划", "中"),
    ("Why the stock is moving today", "低"),
    ("Approbation award for design team", "低"),             # "probe" must not match inside another word
])
def test_keyword_levels(title, level):
    assert score_item(title).level == level


def test_keyword_only_in_body_counts_one_level_lower():
    r = score_item("Company update", "Sources say the DOJ subpoena was received last week")
    assert r.level == "中" and "subpoena" in r.matched
    assert score_item("Company update", "earnings were in line").level == "低"


def test_headline_mention_ranks_higher():
    terms = ["NVDA", "Nvidia"]
    a = score_item("Nvidia announces buyback", watch_terms=terms)
    b = score_item("Chipmaker announces buyback", watch_terms=terms)
    assert a.level == b.level == "中"
    assert a.score > b.score and a.headline_mentions and not b.headline_mentions


def test_user_keywords_are_used():
    kw = KeywordSet(high=["stock split"], medium=["conference"])
    assert score_item("Board approves stock split", keywords=kw).level == "高"
    assert score_item("FBI probe", keywords=kw).level == "低"   # default list replaced by the user's list


@pytest.mark.parametrize("form, items, level", [
    ("8-K", ["2.02", "9.01"], 3), ("8-K", ["5.02"], 3), ("8-K", ["1.05"], 3), ("8-K", ["8.01"], 2),
    ("6-K", [], 2), ("SC 13D", [], 2), ("SCHEDULE 13D/A", [], 2), ("SCHEDULE 13G", [], 1), ("4", [], 1),
])
def test_sec_rules(form, items, level):
    assert sec_level(form, items)[0] == level


def test_8k_202_always_high_even_without_keywords():
    r = score_item("NVIDIA CORP 8-K (Items 2.02, 9.01)", kind="filing", form="8-K", items=["2.02", "9.01"])
    assert r.level == "高" and "业绩公告" in r.reasons[0]


def test_form_normalization_and_mentions():
    assert normalize_form("SCHEDULE 13G/A") == "SC 13G/A"
    assert mentions("英伟达股价大涨", ["英伟达"]) and not mentions("AMDX rises", ["AMD"])


# --------------------------------------------------------------------- dedup
def test_normalize_and_clean():
    assert clean_title("<em>Nvidia</em> &amp; AMD") == "Nvidia & AMD"
    assert normalize("Nvidia's CFO Steps Down!") == "nvidia s cfo steps down"


def test_url_keys_match_across_sources():
    a = url_key("https://www.moomoo.com/news/post/1004231217?futusource=x")
    b = url_key("https://www.moomoo.com/hans/news/post/1004231217?src=39")
    assert a == b == "moomoo:1004231217"
    assert url_key("https://www.sec.gov/Archives/edgar/data/1/000104581026000001/0001045810-26-000001-index.htm") \
        == "sec:0001045810-26-000001"


def _ev(title, t, symbols=("US.NVDA",), kinds=("news",), keys=(), items=()):
    return {"symbols": list(symbols), "published": t, "last": t, "title": title, "titles": [title],
            "kinds": list(kinds), "keys": list(keys), "cats": sorted(categories(title, items))}


def test_same_event_similar_headlines_within_window():
    ev = _ev("DOJ opens probe into Nvidia executive over export sales", 1000.0)
    item = {"symbols": ["US.NVDA"], "published": 1000.0 + 600, "title": "Nvidia executive under DOJ probe, sources say",
            "url": "", "kind": "news"}
    assert similarity(item["title"], ev["title"]) >= 0.5
    assert same_event(item, ev)


def test_rephrased_headline_with_short_ticker_is_grouped():
    a = "DOJ opens probe into QQQ executive over export sales"
    b = "QQQ executive under DOJ probe, sources say"
    assert similarity(a, b) >= 0.5
    assert similarity("QQQ to present at technology conference", "DOJ opens probe into QQQ executive") < 0.5


def test_not_same_event_outside_window_or_other_symbol():
    ev = _ev("Nvidia CFO steps down", 1000.0)
    assert not same_event({"symbols": ["US.NVDA"], "published": 1000.0 + 31 * 60, "title": "Nvidia CFO steps down",
                           "url": "", "kind": "news"}, ev)
    assert not same_event({"symbols": ["US.AMD"], "published": 1000.0, "title": "Nvidia CFO steps down",
                           "url": "", "kind": "news"}, ev)


def test_same_article_key_matches_even_with_different_titles():
    ev = _ev("英伟达高管遭调查", 1000.0, keys=["moomoo:42"])
    item = {"symbols": ["US.NVDA"], "published": 1200.0, "title": "Nvidia executive investigated",
            "url": "https://www.moomoo.com/news/post/42", "kind": "news"}
    assert same_event(item, ev)


def test_filing_groups_with_matching_news_category():
    filing = _ev("NVIDIA CORP 8-K (Items 2.02, 9.01)", 1000.0, kinds=["filing"], items=["2.02"])
    news = {"symbols": ["US.NVDA"], "published": 1300.0, "title": "Nvidia third-quarter results beat estimates",
            "url": "", "kind": "news"}
    other = {"symbols": ["US.NVDA"], "published": 1300.0, "title": "Nvidia opens new office in Taipei",
             "url": "", "kind": "news"}
    assert same_event(news, filing)
    assert not same_event(other, filing)


def test_find_event_picks_closest():
    e1 = _ev("Nvidia CFO steps down", 1000.0)
    e2 = _ev("Nvidia CFO steps down", 2500.0)
    item = {"symbols": ["US.NVDA"], "published": 2400.0, "title": "Nvidia CFO steps down", "url": "", "kind": "news"}
    assert find_event(item, [e1, e2]) is e2


def test_ticker_relevance_filter_for_short_tickers():
    from backend.news.service import NewsService
    f = NewsService.headline_mentions_ticker
    assert not f("Neuronetics: Gross Proceeds Expected to Be Approximately $30M", "BE", set())
    assert f("BE shares jump after earnings", "BE", set())
    assert f("$BE rallies", "BE", set())
    assert f("Why Is Bloom Energy Stock Sliding Today?", "BE", {"Bloom Energy"})
    assert f("nvidia beats estimates", "NVDA", {"Nvidia"}) and f("Traders eye nvda", "NVDA", set())


# --------------------------------------------------------------- SEC limiter
def test_sec_rate_limiter_never_exceeds_5_per_second():
    async def main():
        lim = make_http_limiter(sec_per_sec=5)
        stamps = []

        async def one():
            await lim.acquire("sec_edgar")
            stamps.append(time.monotonic())

        t0 = time.monotonic()
        await asyncio.gather(*(one() for _ in range(15)))
        return t0, sorted(stamps)

    t0, s = asyncio.run(main())
    for i in range(len(s) - 5):
        assert s[i + 5] - s[i] >= 1.0 - TOL, "6 SEC requests inside one second"
    assert s[-1] - t0 >= 2.0 - TOL


def test_sec_limiter_is_capped_at_official_10_per_second():
    assert make_http_limiter(sec_per_sec=50).usage()["sec_edgar"]["max_calls"] == 10


def test_sec_user_agent_needs_email():
    with pytest.raises(SourceError):
        SecEdgar.user_agent("app", "")
    assert SecEdgar.user_agent("app", "someone@example.com") == "app someone@example.com"


def test_parse_submissions():
    data = {"name": "NVIDIA CORP", "filings": {"recent": {
        "accessionNumber": ["0001045810-26-000010", "0001045810-26-000009", "0001045810-26-000008", "0001-26-1"],
        "form": ["8-K", "4", "SCHEDULE 13G", "8-K"],
        "items": ["2.02,9.01", "", "", "8.01"],
        "acceptanceDateTime": ["2026-11-10T21:20:00.000Z", "2026-11-10T21:00:00.000Z",
                               "2026-11-09T12:00:00.000Z", "2026-10-01T12:00:00.000Z"],
        "filingDate": ["2026-11-10", "2026-11-10", "2026-11-09", "2026-10-01"]}}}
    since = 1794200000.0  # 2026-11-09 ~
    out = SecEdgar.parse_submissions(data, "US.NVDA", 1045810, ["8-K", "SC 13G"], since)
    assert [o["form"] for o in out] == ["8-K", "SCHEDULE 13G"]       # Form 4 not watched, old 8-K too old
    assert out[0]["items"] == ["2.02", "9.01"] and "业绩公告" in out[0]["summary"]
    assert out[0]["url"].endswith("/000104581026000010/0001045810-26-000010-index.htm")


def test_parse_moomoo_date_only():
    ts, precision = parse_moomoo_date("2020/1/2")
    assert precision == "date" and ts > 0


# ------------------------------------------------------------- US stocks only
@pytest.mark.parametrize("title, non_us", [
    ("We Think You Should Be Aware Of Some Concerning Factors In Three Squirrels' (SZSE:300783)", True),
    ("Shengyuan Environmental ProtectionLtd (SZSE:300867) Posted Healthy Earnings", True),
    ("贵州茅台（600519.SH）发布公告", True),
    ("Tencent (0700.HK) shares rise", True),
    ("Toyota (TSE:7203) cuts outlook", True),
    ("Nvidia (NVDA) beats estimates", False),
    ("Apple sells 10.5 million units in China", False),
])
def test_non_us_listings_are_detected(title, non_us):
    from backend.news.sources import is_non_us
    assert is_non_us(title) is non_us


def test_opend_news_with_only_non_us_securities_is_dropped():
    from backend.news.sources import from_opend_news, us_related
    assert us_related(["US.NVDA", "LITE.US", "SZ.300783", "HK.00700"]) == ["US.NVDA", "US.LITE"]
    assert from_opend_news({"title": "A股公告", "related_securities": ["SZ.300783"], "url": ""}, "US.BE") is None
    kept = from_opend_news({"title": "Nvidia news", "related_securities": ["US.NVDA", "HK.00700"], "url": ""}, "US.NVDA")
    assert kept["symbols"] == ["US.NVDA"]


def test_store_delete_items_removes_orphan_events(tmp_path):
    from backend.news.store import NewsStore
    st = NewsStore(tmp_path / "n.db")
    st.save_event({"id": "e1", "symbols": ["US.BE"], "title": "t", "titles": ["t"], "published": 1, "last": 1,
                   "level": "低", "count": 1, "sources": [], "kinds": [], "keys": [], "cats": [], "reasons": []})
    st.save_event({"id": "e2", "symbols": ["US.BE"], "title": "u", "titles": ["u"], "published": 1, "last": 1,
                   "level": "低", "count": 1, "sources": [], "kinds": [], "keys": [], "cats": [], "reasons": []})
    st.add_item({"id": "a", "event_id": "e1", "symbols": ["US.BE"], "title": "t", "published": 1})
    st.add_item({"id": "b", "event_id": "e2", "symbols": ["US.BE"], "title": "u", "published": 1})
    assert st.delete_items(["a"]) == 1
    assert st.get_event("e1") is None and st.get_event("e2") is not None
    st.close()


# ------------------------------------------- SEC: where companies are based (US-only market view)
def test_sec_region_and_ticker_parsing():
    assert SecEdgar.region_of(["Beijing"]) == "china"
    assert SecEdgar.region_of(["Chaoyang District, Beijing, F4"]) == "china"
    assert SecEdgar.region_of(["Hong Kong, K3"]) == "china" and SecEdgar.region_of(["Macau"]) == "china"
    assert SecEdgar.region_of(["Hsinchu, F5"]) == "taiwan" and SecEdgar.region_of(["Taipei City"]) == "taiwan"
    for loc in (["Singapore, U0"], ["Dublin"], ["New York, NY"], ["Tokyo, M0"], []):
        assert SecEdgar.region_of(loc) is None, loc
    hits = [{"_source": {"display_names": ["Alibaba Group Holding Ltd  (BABA, BABAF)  (CIK 0001577552)",
                                           "No Ticker Ltd  (CIK 0000000002)"]}}]
    assert SecEdgar.parse_display_tickers(hits) == {"BABA", "BABAF"}


def test_sec_located_tickers_uses_codes_and_recent_place_names():
    sec = SecEdgar(None, None, None)
    seen = []

    def hit(name, tick, loc):
        return {"_source": {"display_names": [f"{name}  ({tick})  (CIK 0000000001)"], "biz_locations": [loc]}}

    async def fake_get(url, ua):
        seen.append(url)
        if "locationCodes=F4" in url and "forms=20-F" in url:
            hits = [hit("Baidu", "BIDU", "Beijing, F4")]
        elif "locationCodes=F5" in url and "forms=20-F" in url:
            hits = [hit("TSMC", "TSM", "Hsinchu, F5")]
        elif "locationCodes" not in url:   # recent 20-F filings: city only, no code
            hits = [hit("New Co", "NEWC", "Shenzhen"), hit("Sea", "SE", "Singapore"), hit("UMC", "UMC", "Taipei")]
        else:
            hits = []
        if "from=0" not in url:
            hits = []
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    sec._get = fake_get
    out = asyncio.run(sec.located_tickers("app someone@example.com"))
    assert out == {"china": {"BIDU", "NEWC"}, "taiwan": {"TSM", "UMC"}, "failed": 0}
    assert all(u.startswith("https://efts.sec.gov/LATEST/search-index?") for u in seen)


def test_sec_located_tickers_retries_and_reports_failed_queries(monkeypatch):
    import httpx
    sec = SecEdgar(None, None, None)
    calls = {"n": 0}

    async def fake_get(url, ua):
        calls["n"] += 1
        if "locationCodes=K3" in url and "forms=20-F" in url:          # always broken
            raise httpx.HTTPStatusError("500", request=httpx.Request("GET", url), response=httpx.Response(500))
        if "locationCodes=F4" in url and "forms=20-F" in url and calls["n"] == 1:   # broken once
            raise httpx.HTTPStatusError("500", request=httpx.Request("GET", url), response=httpx.Response(500))
        hits = [{"_source": {"display_names": ["Baidu  (BIDU)  (CIK 1)"], "biz_locations": ["Beijing, F4"]}}] \
            if "locationCodes=F4" in url and "from=0" in url else []
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    async def no_sleep(_):
        return None

    sec._get = fake_get
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    out = asyncio.run(sec.located_tickers("app someone@example.com"))
    assert out["china"] == {"BIDU"} and out["failed"] == 1    # F4 recovered on retry; K3 20-F reported
