"""News sources.

A  moomoo News Search skill: GET https://ai-news-search.moomoo.com/news_search (public, no key,
   no documented rate limit -> polled conservatively, 1 request per target every 2 minutes).
B  moomoo API get_search_news via OpenD (10 / 30 s). publish_time is a date only.
C  SEC EDGAR submissions API (User-Agent with contact email required, <= 5 requests / s here,
   official cap 10 / s). Ticker -> CIK from https://www.sec.gov/files/company_tickers.json.
D  Earnings calendar via OpenD get_earnings_calendar (60 / 30 s, range <= 7 days) - in service.py.
Mock  simulated news and filings for --mock.
Every source returns plain item dicts (see NewsItem keys below).
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..ratelimit import Limit, SlidingWindowLimiter
from ..timeutil import ET, et_date_str
from .dedup import clean_title, url_key
from .scoring import SEC_ITEM_ZH, normalize_form

DEFAULT_SEC_FORMS = ["8-K", "8-K/A", "6-K", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"]
NEWS_TYPE = {1: ("news", "moomoo 资讯"), 2: ("notice", "moomoo 公告"), 3: ("research", "moomoo 研报")}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# Non-US listings in headlines: "(SZSE:300783)", "(HKEX:700)", "600519.SH", "0700.HK", "SZ.300783" ...
_NON_US_EXCH = re.compile(r"[(（]\s*(?:SZSE|SHSE|SSE|SEHK|HKEX|HKG|TSE|TYO|KOSE|KOSDAQ|KRX|LSE|ASX|TSX|TSXV|NSE|BSE|"
                          r"SGX|TWSE|TPEX|XETRA|ETR|EPA|FRA|BIT|BME|SIX|OM|CPSE|OB|JSE|BMV|BOVESPA|B3)\s*[:：]",
                          re.IGNORECASE)
_NON_US_CODE = re.compile(r"(?<![A-Za-z0-9])(?:\d{6}\.(?:SZ|SH|SS)|\d{4,5}\.HK|HK\.\d{4,5}|(?:SZ|SH)\.\d{6})(?![A-Za-z0-9])",
                          re.IGNORECASE)


def is_non_us(title: str) -> bool:
    """True when the headline is about a security listed outside the US (A-shares, HK, Japan ...)."""
    return bool(_NON_US_EXCH.search(title or "") or _NON_US_CODE.search(title or ""))


def us_related(related: list[str]) -> list[str]:
    """moomoo related_securities -> US codes only ('US.NVDA', 'LITE.US' -> 'US.LITE')."""
    out = []
    for s in related or []:
        s = str(s).strip().upper()
        if s.startswith("US."):
            out.append(s)
        elif s.endswith(".US"):
            out.append("US." + s[:-3])
    return out


class SourceError(Exception):
    pass


def make_http_limiter(moomoo_per_min: int = 20, sec_per_sec: int = 5) -> SlidingWindowLimiter:
    """Separate limiter for the HTTP sources (not OpenD)."""
    return SlidingWindowLimiter({"moomoo_news_search": Limit(max(1, moomoo_per_min), 60.0),
                                 "sec_edgar": Limit(max(1, min(sec_per_sec, 10)), 1.0)})


def ticker_of(code: str) -> str:
    return code.split(".", 1)[1] if code.startswith("US.") else code


# --------------------------------------------------------------------- A
class MoomooNewsSearch:
    URL = "https://ai-news-search.moomoo.com/news_search"

    def __init__(self, http: httpx.AsyncClient, limiter: SlidingWindowLimiter, user_agent: str):
        self.http, self.limiter, self.ua = http, limiter, user_agent

    async def fetch(self, keyword: str, news_type: int = 1, size: int = 20, lang: str = "en",
                    symbols: list[str] | None = None, origin: str = "watch") -> list[dict]:
        await self.limiter.acquire("moomoo_news_search")
        r = await self.http.get(self.URL, headers={"User-Agent": self.ua},
                                params={"keyword": keyword, "size": max(1, min(int(size), 50)),
                                        "news_type": news_type, "lang": lang, "sort_type": 2})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise SourceError(f"moomoo 资讯接口返回 code={data.get('code')}：{data.get('message')}")
        kind, name = NEWS_TYPE.get(int(news_type), ("news", "moomoo 资讯"))
        out = []
        for d in data.get("data") or []:
            try:
                pt = float(d.get("publish_time") or 0)
            except (TypeError, ValueError):
                pt = 0.0
            if pt > 1e12:
                pt /= 1000
            title = clean_title(d.get("title", ""))
            if not title:
                continue
            out.append({"source": "moomoo_search", "key": str(d.get("news_id") or d.get("url")), "source_name": name,
                        "kind": kind, "symbols": list(symbols or []), "title": title, "summary": "",
                        "url": d.get("url") or "", "lang": lang, "published": pt or time.time(),
                        "time_precision": "second" if pt else "unknown", "form": "", "items": [], "origin": origin})
        return out


# --------------------------------------------------------------------- B
def parse_moomoo_date(text: str, now: float | None = None) -> tuple[float, str]:
    """'2026/11/10' (date only) -> (timestamp, precision). Today's items get the time first seen."""
    now = now or time.time()
    try:
        y, m, d = (int(x) for x in str(text).replace("-", "/").split("/")[:3])
        day = dt.date(y, m, d)
    except (TypeError, ValueError):
        return now, "unknown"
    today = dt.datetime.fromtimestamp(now, ET).date()
    if day >= today:
        return now, "date"
    return dt.datetime.combine(day, dt.time(12, 0), ET).timestamp(), "date"


def from_opend_news(rec: dict, symbol: str, origin: str = "watch") -> dict | None:
    title = clean_title(rec.get("title", ""))
    if not title:
        return None
    ts, precision = parse_moomoo_date(rec.get("publish_time"))
    st = str(rec.get("news_sub_type") or "NEWS")
    raw_related = rec.get("related_securities") or []
    related = us_related(raw_related)
    if raw_related and not related:
        return None  # only non-US securities (A-shares, HK, ...): not shown, the dashboard is US only
    syms = sorted(set(related) | {symbol}) if symbol else related
    url = rec.get("url") or ""
    return {"source": "moomoo_api", "key": url_key(url) or title, "source_name": rec.get("source") or "moomoo",
            "kind": "notice" if st == "NOTICE" else "news", "symbols": syms, "title": title, "summary": "",
            "url": url, "lang": "zh", "published": ts, "time_precision": precision, "form": "", "items": [],
            "origin": origin}


# --------------------------------------------------------------------- C
class SecEdgar:
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    SUB_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self, http: httpx.AsyncClient, limiter: SlidingWindowLimiter, cache_dir: Path):
        self.http, self.limiter, self.cache_dir = http, limiter, cache_dir
        self._tickers: dict[str, dict] = {}
        self._tickers_day: str | None = None

    @staticmethod
    def user_agent(app_name: str, email: str) -> str:
        if not EMAIL_RE.match(email or ""):
            raise SourceError("SEC EDGAR 需要联系邮箱：请在设置中填写")
        return f"{app_name} {email}"

    async def _get(self, url: str, ua: str) -> Any:
        await self.limiter.acquire("sec_edgar")
        r = await self.http.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        if r.status_code == 403:
            raise SourceError("SEC 拒绝访问（HTTP 403）。请确认联系邮箱有效，且请求频率未超过限制")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def ticker_map(self, ua: str) -> dict[str, dict]:
        today = et_date_str()
        if self._tickers and self._tickers_day == today:
            return self._tickers
        path = self.cache_dir / f"sec_company_tickers_{today}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = await self._get(self.TICKERS_URL, ua) or {}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw), encoding="utf-8")
            for old in path.parent.glob("sec_company_tickers_*.json"):
                if old != path:
                    old.unlink(missing_ok=True)
        self._tickers = {str(v["ticker"]).upper(): {"cik": int(v["cik_str"]), "title": v.get("title", "")}
                         for v in raw.values()}
        self._tickers_day = today
        return self._tickers

    @staticmethod
    def parse_submissions(data: dict, symbol: str, cik: int, forms: list[str], since_ts: float) -> list[dict]:
        rec = (data or {}).get("filings", {}).get("recent", {})
        accs = rec.get("accessionNumber") or []
        wanted = {normalize_form(f) for f in forms}
        name = data.get("name") or symbol
        out = []
        for i, acc in enumerate(accs):
            form = (rec.get("form") or [""] * len(accs))[i]
            if normalize_form(form) not in wanted:
                continue
            accepted = (rec.get("acceptanceDateTime") or [""] * len(accs))[i]
            try:
                ts = dt.datetime.fromisoformat(accepted.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                fd = (rec.get("filingDate") or [""] * len(accs))[i]
                try:
                    ts = dt.datetime.combine(dt.date.fromisoformat(fd), dt.time(16), ET).timestamp()
                except ValueError:
                    continue
            if ts < since_ts:
                continue
            items = [x.strip() for x in ((rec.get("items") or [""] * len(accs))[i] or "").split(",") if x.strip()]
            title = f"{name} {form}" + (f" (Items {', '.join(items)})" if items else "")
            summary = "；".join(f"Item {x} {SEC_ITEM_ZH.get(x, '')}".strip() for x in items)
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"
            out.append({"source": "sec", "key": acc, "source_name": "SEC EDGAR", "kind": "filing", "symbols": [symbol],
                        "title": title, "summary": summary, "url": url, "lang": "en", "published": ts,
                        "time_precision": "second", "form": form, "items": items, "origin": "watch"})
        return out

    async def filings(self, symbol: str, ua: str, forms: list[str], since_ts: float) -> list[dict]:
        tickers = await self.ticker_map(ua)
        info = tickers.get(ticker_of(symbol).upper().replace(".", "-")) or tickers.get(ticker_of(symbol).upper())
        if not info:
            return []  # ETFs and some trusts have no company CIK in the map
        data = await self._get(self.SUB_URL.format(cik=info["cik"]), ua)
        return self.parse_submissions(data or {}, symbol, info["cik"], forms, since_ts)

    # Where SEC registrants are based, from EDGAR full-text search of annual reports (20-F / 10-K).
    # Older filings carry the EDGAR location code (F4 mainland China, K3 Hong Kong, F5 Taiwan); filings
    # from 2026 on often show only the city ("Beijing"), so recent 20-F filings are matched by place name.
    # display_names look like "Alibaba Group Holding Ltd  (BABA, BABAF)  (CIK 0001577552)".
    EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
    LOCATION_CODES = {"china": ("F4", "K3"), "taiwan": ("F5",)}
    _DISPLAY_TICKERS = re.compile(r"\(([A-Z0-9.\-, ]+)\)\s+\(CIK")
    _CHINA_PLACE = re.compile(
        r",\s*(F4|K3)\s*$|\b(China|PRC|Hong\s*Kong|Kowloon|Wan\s*Chai|Sheung\s*Wan|Quarry\s*Bay|Causeway\s*Bay|"
        r"Tsim\s*Sha\s*Tsui|Kwun\s*Tong|Sha\s*Tin|Tsuen\s*Wan|Macau|Macao|Beijing|Shanghai|Shenzhen|Guangzhou|"
        r"Hangzhou|Nanjing|Suzhou|Wuhan|Chengdu|Chongqing|Tianjin|Xiamen|Fuzhou|Qingdao|Dalian|Xi'?an|Changsha|Hefei|"
        r"Jinan|Zhengzhou|Ningbo|Wuxi|Dongguan|Foshan|Zhuhai|Kunming|Nanning|Harbin|Shenyang|Changchun|Taiyuan|"
        r"Shijiazhuang|Nanchang|Guiyang|Lanzhou|Urumqi|Hohhot|Haikou|Wenzhou|Jiaxing|Shaoxing|Jinhua|Changzhou|"
        r"Yangzhou|Nantong|Xuzhou|Guangdong|Zhejiang|Jiangsu|Fujian|Shandong|Hubei|Hunan|Henan|Hebei|Sichuan|Anhui|"
        r"Jiangxi|Yunnan|Guangxi|Shaanxi|Shanxi|Liaoning|Jilin|Heilongjiang|Hainan|Guizhou|Gansu|Inner\s*Mongolia|"
        r"Xinjiang|Ningxia|Qinghai)\b", re.IGNORECASE)
    _TAIWAN_PLACE = re.compile(r",\s*F5\s*$|\b(Taiwan|Taipei|Hsinchu|Kaohsiung|Taichung|Taoyuan|Tainan)\b", re.IGNORECASE)

    @classmethod
    def parse_display_tickers(cls, hits: list[dict]) -> set[str]:
        out: set[str] = set()
        for h in hits:
            for dn in (h.get("_source") or {}).get("display_names") or []:
                m = cls._DISPLAY_TICKERS.search(dn)
                if m:
                    out.update(t.strip().replace("-", ".") for t in m.group(1).split(",") if t.strip())
        return out

    @classmethod
    def region_of(cls, biz_locations: list[str]) -> str | None:
        """'taiwan' wins over 'china' (e.g. "Taipei" never counts as mainland China)."""
        locs = [str(x) for x in biz_locations or []]
        if any(cls._TAIWAN_PLACE.search(x) for x in locs):
            return "taiwan"
        if any(cls._CHINA_PLACE.search(x) for x in locs):
            return "china"
        return None

    async def _efts_page(self, url: str, ua: str, attempts: int = 3) -> dict:
        """EDGAR full-text search sometimes answers 500 for one page: retry with a short backoff."""
        import asyncio
        for i in range(attempts):
            try:
                return await self._get(url, ua) or {}
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                status = getattr(getattr(e, "response", None), "status_code", 0)
                if i == attempts - 1 or (status and status < 500):
                    raise
                await asyncio.sleep(2.0 * (i + 1))
        return {}

    async def _efts_all(self, ua: str, q: dict, cap: int = 5000) -> list[dict]:
        from urllib.parse import urlencode
        out: list[dict] = []
        while True:
            data = await self._efts_page(f"{self.EFTS_URL}?{urlencode({**q, 'from': len(out)})}", ua)
            hits = (data.get("hits") or {}).get("hits") or []
            total = int(((data.get("hits") or {}).get("total") or {}).get("value") or 0)
            out += hits
            if not hits or len(out) >= min(total, cap):
                return out

    async def located_tickers(self, ua: str, coded_months: int = 36, recent_months: int = 12) -> dict[str, set[str]]:
        """{"china": tickers based in mainland China / Hong Kong / Macau, "taiwan": tickers based in Taiwan,
        "failed": number of queries that still failed after retries (their results are missing)}.
        About 30 requests (SEC allows 10 per second; the shared limiter keeps this at 5)."""
        end = dt.date.fromisoformat(et_date_str())
        out: dict[str, Any] = {"china": set(), "taiwan": set(), "failed": 0}
        start = (end - dt.timedelta(days=int(coded_months * 30.5))).isoformat()
        queries: list[tuple[str | None, dict, int]] = [
            (region, {"forms": form, "locationCodes": loc, "dateRange": "custom", "startdt": start,
                      "enddt": end.isoformat()}, 5000)
            for region, codes in self.LOCATION_CODES.items() for loc in codes for form in ("20-F", "10-K")]
        recent = (end - dt.timedelta(days=int(recent_months * 30.5))).isoformat()
        queries.append((None, {"forms": "20-F", "dateRange": "custom", "startdt": recent, "enddt": end.isoformat()}, 10000))
        last_error: Exception | None = None
        for region, q, cap in queries:
            try:
                hits = await self._efts_all(ua, q, cap)
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                out["failed"] += 1
                last_error = e
                continue
            for h in hits:
                r = region or self.region_of((h.get("_source") or {}).get("biz_locations") or [])
                if r:
                    out[r] |= self.parse_display_tickers([h])
        if out["failed"] == len(queries) and last_error:
            raise last_error
        out["china"] -= out["taiwan"]
        return out

    async def company_title(self, symbol: str, ua: str) -> str | None:
        try:
            info = (await self.ticker_map(ua)).get(ticker_of(symbol).upper())
        except SourceError:
            return None
        return info["title"] if info else None


# ------------------------------------------------------------------ mock
class MockNews:
    """Simulated news + filings so the page works without network access."""

    TEMPLATES = [
        (3, "DOJ opens probe into {n} executive over export sales"),
        (3, "{n} CFO steps down effective immediately"),
        (3, "Commerce Department adds {n} supplier to Entity List"),
        (3, "{n} announces guidance raise after record quarter"),
        (2, "{n} shares rise as analysts lift price target"),
        (2, "{n} announces $5 billion buyback program"),
        (2, "{n} expands partnership with major cloud provider"),
        (1, "Why {n} stock is moving today"),
        (1, "{n} to present at upcoming technology conference"),
        (3, "{n}高管遭美国司法部调查"),
        (2, "{n}宣布新的回购计划"),
        (1, "{n}股价盘中震荡"),
    ]
    VARIANT = {"DOJ opens probe into {n} executive over export sales": "{n} executive under DOJ probe, sources say",
               "{n} CFO steps down effective immediately": "{n} says CFO steps down; search for successor begins"}
    OUTLETS = ["Benzinga", "MT Newswires", "Reuters (模拟)", "Dow Jones (模拟)"]

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.n = 0
        self._first = True   # the first round always contains one event reported by two outlets (dedup demo)

    def generate(self, symbols: list[str], names: dict[str, str], now: float | None = None) -> list[dict]:
        now = now or time.time()
        out: list[dict] = []
        if not symbols:
            return out
        for i in range(1 if self._first else self.rng.choice([0, 1, 1, 2])):
            sym = self.rng.choice(symbols)
            n = names.get(sym) or ticker_of(sym)
            lvl, tpl = self.TEMPLATES[0] if self._first else self.rng.choice(self.TEMPLATES)
            self._first = False
            self.n += 1
            title = tpl.format(n=n)
            out.append(self._item(sym, title, now - self.rng.uniform(0, 120), self.rng.choice(self.OUTLETS)))
            if tpl in self.VARIANT and (i == 0 and self.n <= 2 or self.rng.random() < 0.8):  # same event, 2nd outlet
                self.n += 1
                out.append(self._item(sym, self.VARIANT[tpl].format(n=n), now - self.rng.uniform(0, 300),
                                      self.rng.choice(self.OUTLETS)))
        if self.rng.random() < 0.18:
            sym = self.rng.choice(symbols)
            items = self.rng.choice([["2.02", "9.01"], ["5.02"], ["8.01"], ["7.01", "9.01"]])
            self.n += 1
            acc = f"0000000000-26-{self.n:06d}"
            out.append({"source": "mock", "key": f"sec-{acc}", "source_name": "SEC EDGAR（模拟）", "kind": "filing",
                        "symbols": [sym], "title": f"{names.get(sym) or ticker_of(sym)} 8-K (Items {', '.join(items)})",
                        "summary": "；".join(f"Item {x} {SEC_ITEM_ZH.get(x, '')}" for x in items),
                        "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker_of(sym)}&type=8-K#mock-{acc}",
                        "lang": "en", "published": now, "time_precision": "second", "form": "8-K", "items": items,
                        "origin": "watch"})
        return out

    def _item(self, sym: str, title: str, ts: float, outlet: str) -> dict:
        nid = 1004900000 + self.n
        return {"source": "mock", "key": str(nid), "source_name": f"{outlet}（模拟）", "kind": "news", "symbols": [sym],
                "title": title, "summary": "", "url": f"https://www.moomoo.com/news/post/{nid}#mock",
                "lang": "zh" if re.search(r"[㐀-鿿]", title) else "en", "published": ts,
                "time_precision": "second", "form": "", "items": [], "origin": "watch"}
