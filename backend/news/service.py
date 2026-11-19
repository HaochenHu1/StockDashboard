"""News and event monitor: polling, scoring, dedup, storage, notifications, settings."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..alerts import normalize_code
from ..errors import UserError
from ..gateway.base import OpenDError, is_option_code, parse_option_code
from ..ratelimit import Priority
from ..timeutil import ET, et_date_str
from .classifier import Layer2Classifier
from .dedup import categories, find_event, url_key
from .scoring import (DEFAULT_HIGH_KEYWORDS, DEFAULT_MEDIUM_KEYWORDS, DEFAULT_SEC_HIGH_ITEMS, LEVEL_NUM, LEVELS,
                      SEC_ITEM_ZH, KeywordSet, score_item)
from .sources import (DEFAULT_SEC_FORMS, EMAIL_RE, MockNews, MoomooNewsSearch, SecEdgar, SourceError,
                      from_opend_news, is_non_us, make_http_limiter, ticker_of)
from .store import NewsStore

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)
SOURCE_LABELS = {"moomoo_search": "moomoo 资讯搜索", "moomoo_api": "moomoo API 资讯", "sec": "SEC EDGAR",
                 "earnings": "财报日历", "layer2": "智能分类（Claude）", "mock": "模拟资讯"}
PUB_ZH = {"BEFORE": "盘前", "AFTER": "盘后", "REGULAR": "盘中", "N/A": "未知"}
NAME_SUFFIX = (" CORPORATION", " CORP", " INCORPORATED", " INC", " HOLDINGS", " LTD", " PLC", " CO", " N.V.", " SA")


def _fmt_et(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d %H:%M:%S") if ts else None


class NewsService:
    def __init__(self, app: "AppState", data_dir: Path):
        self.app = app
        self.data_dir = data_dir
        self.store = NewsStore(data_dir / "news.db")
        lim = self.c("http_rate_limits", {}) or {}
        self.http_limiter = make_http_limiter(int((lim.get("moomoo_news_search") or {}).get("max_calls", 20)),
                                              int(self.c("sources", {}).get("sec", {}).get("max_requests_per_sec", 5)))
        self.http: httpx.AsyncClient | None = None
        self.search: MoomooNewsSearch | None = None
        self.sec: SecEdgar | None = None
        self.mock = MockNews() if app.mock else None
        self.classifier = Layer2Classifier(lambda k, d: (self.c("layer2", {}) or {}).get(k, d), self.store)
        self.status: dict[str, dict] = {k: {"last_ok": None, "last_error": None, "error_at": None, "polls": 0}
                                        for k in SOURCE_LABELS}
        self._earnings_all: list[dict] = []
        self._sec_since: dict[str, float] = {}
        self._leader_seen: dict[str, float] = {}
        self._terms: dict[str, set[str]] = {}
        self._sec_names: dict[str, set[str]] = {}
        self._kw = KeywordSet()
        self._tasks: list[asyncio.Task] = []
        self._l2_lock = asyncio.Lock()
        self.reload_keywords()

    # ----------------------------------------------------------------- config
    def c(self, key: str, default: Any = None) -> Any:
        v = self.app.cfg.get("news", key, default=default)
        return default if v is None else v

    def src(self, name: str) -> dict:
        return (self.c("sources", {}) or {}).get(name, {}) or {}

    def enabled(self, name: str) -> bool:
        if name == "mock":
            return self.app.mock
        if name == "layer2":
            return bool((self.c("layer2", {}) or {}).get("enabled", False))
        if self.app.mock and name in ("moomoo_search", "sec"):
            return False  # mock mode never calls the network sources
        return bool(self.src(name).get("enabled", True))

    def reload_keywords(self) -> None:
        kw = self.c("keywords", {}) or {}
        self._kw = KeywordSet(list(kw.get("high") or DEFAULT_HIGH_KEYWORDS), list(kw.get("medium") or DEFAULT_MEDIUM_KEYWORDS))

    # --------------------------------------------------------------- symbols
    def watch_symbols(self) -> list[str]:
        out = list(self.app.stocks.codes())
        for u in self.app.options.underlyings:
            out.append(u["code"])
        for p in self.app.options.pinned:
            info = parse_option_code(p)
            if info:
                out.append(info["underlying"])
        return [c for c in dict.fromkeys(out) if not is_option_code(c)]

    def extra_keywords(self) -> list[str]:
        return [k for k in (self.c("extra_keywords", []) or []) if str(k).strip()]

    def aliases(self) -> dict[str, str]:
        raw = self.c("symbol_aliases", {}) or {}
        return {normalize_code(k): str(v).strip() for k, v in raw.items() if str(v).strip()}

    def search_keyword(self, symbol: str) -> tuple[str, bool]:
        """(keyword, is_name). Company names give far better results than short tickers
        ("BE" matches the English word "be"); order: user alias > SEC company name > ticker."""
        alias = self.aliases().get(symbol)
        if alias:
            return alias, True
        sec_name = next(iter(sorted(self._sec_names.get(symbol, set()))), None)
        if sec_name:
            return sec_name, True
        return ticker_of(symbol), False

    @staticmethod
    def headline_mentions_ticker(title: str, ticker: str, names: set[str]) -> bool:
        if len(ticker) <= 3:  # short tickers must appear in upper case as a word, $TICKER or (TICKER)
            if re.search(r"(?<![A-Za-z0-9])\$?" + re.escape(ticker) + r"(?![A-Za-z0-9])", title):
                return True
        elif re.search(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])", title, re.IGNORECASE):
            return True
        return any(n and len(n) >= 3 and n.lower() in title.lower() for n in names)

    def terms(self, symbols: list[str]) -> set[str]:
        out: set[str] = set()
        for s in symbols:
            if s.startswith("KW:"):
                out.add(s[3:])
                continue
            out.add(ticker_of(s))
            q = self.app.store.get(s) or {}
            if q.get("name"):
                out.add(q["name"])
            out |= self._terms.get(s, set()) | self._sec_names.get(s, set())
            if self.aliases().get(s):
                out.add(self.aliases()[s])
        return out

    # --------------------------------------------------------------- lifecycle
    def cleanup_stored(self) -> int:
        """Remove stored items that today's rules would not accept (non-US listings, short-ticker noise)."""
        drop = []
        for it in self.store.all_items():
            if is_non_us(it["title"]):
                drop.append(it["id"])
                continue
            syms = [s for s in it["symbols"] if s.startswith("US.")]
            if it["source"] == "moomoo_search" and it.get("origin") in ("watch", "leader") and syms:
                tk = ticker_of(syms[0])
                names = {self.aliases().get(syms[0], "")} - {""}
                if len(tk) <= 3 and not self.headline_mentions_ticker(it["title"], tk, names):
                    drop.append(it["id"])
        n = self.store.delete_items(drop)
        if n:
            log.info("news: removed %d stored items that are not about US-listed symbols", n)
        return n

    async def start(self) -> None:
        self.cleanup_stored()
        self.http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        ua = str(self.c("user_agent", "moomoo-local-dashboard/1.0 (news monitor)"))
        self.search = MoomooNewsSearch(self.http, self.http_limiter, ua)
        self.sec = SecEdgar(self.http, self.http_limiter, self.data_dir / "cache")
        self._tasks = [asyncio.create_task(self._guard(self._loop_search, "moomoo_search")),
                       asyncio.create_task(self._guard(self._loop_api, "moomoo_api")),
                       asyncio.create_task(self._guard(self._loop_sec, "sec")),
                       asyncio.create_task(self._guard(self._loop_earnings, "earnings")),
                       asyncio.create_task(self._guard(self._loop_purge, "purge"))]
        if self.mock:
            self._tasks.append(asyncio.create_task(self._guard(self._loop_mock, "mock")))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.http:
            await self.http.aclose()
        self.store.close()

    async def _guard(self, fn, name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive; show the error on the status panel
                log.exception("news loop %s crashed", name)
                self._err(name, f"{e.__class__.__name__}: {e}")
                await asyncio.sleep(30)

    def _ok(self, name: str) -> None:
        st = self.status.setdefault(name, {})
        st.update(last_ok=time.time(), last_error=None, polls=st.get("polls", 0) + 1)

    def _err(self, name: str, msg: str) -> None:
        st = self.status.setdefault(name, {})
        st.update(last_error=str(msg)[:300], error_at=time.time())

    # -------------------------------------------------------- source A loop
    async def _loop_search(self) -> None:
        cycle = 0
        await asyncio.sleep(5)
        while True:
            if not self.enabled("moomoo_search"):
                await asyncio.sleep(10)
                continue
            cfg = self.src("moomoo_search")
            interval = max(30.0, float(cfg.get("poll_interval_sec", 120)))
            types = [int(t) for t in cfg.get("news_types", [1, 2])] or [1]
            ntype = types[cycle % len(types)]
            cycle += 1
            targets = [(self.search_keyword(s)[0], ntype, [s], "watch") for s in self.watch_symbols()]
            targets += [(k, 1, [f"KW:{k}"], "keyword") for k in self.extra_keywords()]
            now = time.time()
            market = getattr(self.app, "market", None)
            for code in (market.leader_codes() if market else [])[:30]:
                if len([t for t in targets if t[3] == "leader"]) >= 3:
                    break
                if code not in self.watch_symbols() and now - self._leader_seen.get(code, 0) > 1800:
                    self._leader_seen[code] = now
                    targets.append((ticker_of(code), 1, [code], "leader"))
            if not targets:
                await asyncio.sleep(10)
                continue
            step = interval / len(targets)  # one request per target per interval, spread evenly
            for kw, nt, syms, origin in targets:
                t0 = time.monotonic()
                try:
                    items = await self.search.fetch(kw, nt, int(cfg.get("size", 20)), str(cfg.get("lang", "en")),
                                                    syms, origin)
                    sym = syms[0]
                    if origin in ("watch", "leader") and kw == ticker_of(sym) and len(kw) <= 3:
                        # ticker search: keep only headlines that really mention the symbol
                        names = self.terms([sym]) - {ticker_of(sym)}
                        items = [i for i in items if self.headline_mentions_ticker(i["title"], ticker_of(sym), names)]
                    await self.ingest(items)
                    self._ok("moomoo_search")
                except (httpx.HTTPError, SourceError, ValueError) as e:
                    self._err("moomoo_search", f"{kw}: {e}")
                await asyncio.sleep(max(0.5, step - (time.monotonic() - t0)))

    # -------------------------------------------------------- source B loop
    async def _loop_api(self) -> None:
        cycle = 0
        await asyncio.sleep(8)
        while True:
            if not self.enabled("moomoo_api") or self.app.gw.connection_state() != "connected":
                await asyncio.sleep(10)
                continue
            cfg = self.src("moomoo_api")
            interval = max(60.0, float(cfg.get("poll_interval_sec", 300)))
            sub = ("NEWS", "NOTICE")[cycle % 2]
            cycle += 1
            syms = self.watch_symbols()
            if not syms:
                await asyncio.sleep(10)
                continue
            step = interval / len(syms)
            for s in syms:
                t0 = time.monotonic()
                try:
                    recs = await self.app.client.search_news(ticker_of(s), int(cfg.get("max_count", 20)), sub,
                                                             Priority.BACKGROUND)
                    await self.ingest([x for x in (from_opend_news(r, s) for r in recs) if x])
                    self._ok("moomoo_api")
                except OpenDError as e:
                    self._err("moomoo_api", f"{ticker_of(s)}: {e.msg}")
                await asyncio.sleep(max(0.5, step - (time.monotonic() - t0)))

    # -------------------------------------------------------- source C loop
    def sec_ua(self) -> str:
        return SecEdgar.user_agent(str(self.c("app_name", "moomoo-local-dashboard")), str(self.c("contact_email", "") or ""))

    async def _loop_sec(self) -> None:
        await asyncio.sleep(6)
        while True:
            cfg = self.src("sec")
            interval = max(30.0, float(cfg.get("poll_interval_sec", 60)))
            if not self.enabled("sec"):
                await asyncio.sleep(10)
                continue
            try:
                ua = self.sec_ua()
            except SourceError as e:
                self._err("sec", str(e))
                await asyncio.sleep(10)
                continue
            t0 = time.monotonic()
            forms = list(cfg.get("forms") or DEFAULT_SEC_FORMS)
            forms += ["SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"]  # names since Dec 2024
            if cfg.get("include_form4"):
                forms += ["4", "4/A"]
            lookback = float(cfg.get("initial_lookback_days", 2)) * 86400
            for s in self.watch_symbols():
                since = self._sec_since.get(s, time.time() - lookback)
                try:
                    items = await self.sec.filings(s, ua, forms, since)
                    title = await self.sec.company_title(s, ua)
                    if title:
                        core = title.upper()
                        for suf in NAME_SUFFIX:
                            core = core.replace(suf, "")
                        self._sec_names.setdefault(s, set()).add(core.strip(" ,.").title())
                    await self.ingest(items)
                    newest = max([it["published"] for it in items], default=since)
                    self._sec_since[s] = max(since, min(newest, time.time() - 3600))  # 1 h overlap, dedup by id
                    self._ok("sec")
                except (httpx.HTTPError, SourceError, ValueError) as e:
                    self._err("sec", f"{ticker_of(s)}: {e}")
            await asyncio.sleep(max(5.0, interval - (time.monotonic() - t0)))

    # -------------------------------------------------------- source D loop
    async def _loop_earnings(self) -> None:
        await asyncio.sleep(10)
        while True:
            cfg = self.src("earnings")
            if self.enabled("earnings") and self.app.gw.connection_state() == "connected":
                try:
                    await self.refresh_earnings()
                    self._ok("earnings")
                except OpenDError as e:
                    self._err("earnings", e.msg)
            await asyncio.sleep(max(300.0, float(cfg.get("refresh_sec", 1800))))

    async def refresh_earnings(self) -> None:
        days = max(1, min(int(self.src("earnings").get("days_ahead", 7)), 7))
        today = dt.datetime.now(ET).date()
        recs = await self.app.client.earnings_calendar(today.isoformat(), (today + dt.timedelta(days=days - 1)).isoformat(),
                                                       Priority.BACKGROUND)
        rows = []
        for r in recs:
            code = r.get("security")
            if not code:
                continue
            ts = r.get("earnings_timestamp")
            pub = str(r.get("pub_type") or "N/A")
            rows.append({"code": code, "name": r.get("name"), "date": r.get("earnings_date"),
                         "time_et": _fmt_et(float(ts)) if ts else None, "pub_type": pub, "pub_zh": PUB_ZH.get(pub, pub),
                         "period": r.get("period_text"), "eps_predict": r.get("eps_predict")})
        rows.sort(key=lambda x: (x["date"] or "", x["time_et"] or ""))
        self._earnings_all = rows  # whole US market for the next days; filtered on read so watchlist edits apply at once
        self.app.hub.publish("earnings", self.earnings)

    @property
    def earnings(self) -> list[dict]:
        watch = set(self.watch_symbols())
        return [r for r in self._earnings_all if r["code"] in watch]

    # -------------------------------------------------------- mock / purge
    async def _loop_mock(self) -> None:
        await asyncio.sleep(3)
        while True:
            syms = self.watch_symbols()
            market = getattr(self.app, "market", None)
            syms += [c for c in (market.leader_codes() if market else [])[:8] if c not in syms]  # catalysts
            await self.ingest(self.mock.generate(syms, {s: ticker_of(s) for s in syms}))
            self._ok("mock")
            await asyncio.sleep(float(self.c("mock_interval_sec", 20)))

    async def _loop_purge(self) -> None:
        while True:
            n = self.store.purge(float(self.c("retention_days", 30)))
            if n:
                log.info("news: purged %d items older than retention", n)
            await asyncio.sleep(6 * 3600)

    # ----------------------------------------------------------------- ingest
    async def ingest(self, items: list[dict]) -> None:
        dd = self.c("dedup", {}) or {}
        window = float(dd.get("window_min", 30)) * 60
        threshold = float(dd.get("similarity", 0.5))
        sec_high = list((self.src("sec").get("high_items") or DEFAULT_SEC_HIGH_ITEMS))
        for it in items:
            iid = f"{it['source']}:{it['key']}"
            if self.store.has_item(iid) or is_non_us(it.get("title", "")):
                continue  # already stored, or about a non-US listing (US stocks only)
            now = time.time()
            it.update(id=iid, first_seen=now, ukey=url_key(it.get("url", "")) if it["kind"] != "filing" else f"sec:{it['key']}")
            sc = score_item(it["title"], it.get("summary", ""), kind=it["kind"], form=it.get("form", ""),
                            items=it.get("items", []), watch_terms=self.terms(it["symbols"]), keywords=self._kw,
                            sec_high_items=sec_high)
            it.update(level=sc.level, score=sc.score, matched=sc.matched, reasons=sc.reasons)
            forced = it["kind"] == "filing" and any(i in sec_high for i in it.get("items", []))
            ev = None
            eid = self.store.event_for_key(it["ukey"])
            if eid:
                ev = self.store.get_event(eid)
            if ev is None:
                ev = find_event(it, self.store.recent_events(it["symbols"], it["published"] - window), window, threshold)
            new = ev is None
            if new:
                ev = {"id": uuid.uuid4().hex[:12], "symbols": list(it["symbols"]), "title": it["title"],
                      "titles": [it["title"]], "published": it["published"], "last": it["published"],
                      "l1_level": sc.level, "level": sc.level, "score": sc.score, "l2": None,
                      "sources": [it["source_name"]], "kinds": [it["kind"]], "keys": [it["ukey"]] if it["ukey"] else [],
                      "cats": sorted(categories(it["title"], it.get("items", []))), "count": 1, "notified": 0,
                      "first_seen": now, "reasons": sc.reasons, "forced": 1 if forced else 0}
            else:
                ev["symbols"] = sorted(set(ev["symbols"]) | set(it["symbols"]))
                if it["title"] not in ev["titles"]:
                    ev["titles"] = (ev["titles"] + [it["title"]])[:6]
                ev["published"] = min(ev["published"], it["published"])
                ev["last"] = max(ev["last"], it["published"])
                if LEVEL_NUM[sc.level] > LEVEL_NUM.get(ev["l1_level"], 1):
                    ev["l1_level"] = sc.level
                    ev["reasons"] = sc.reasons
                ev["score"] = max(ev["score"] or 0, sc.score)
                ev["sources"] = sorted(set(ev["sources"]) | {it["source_name"]})
                ev["kinds"] = sorted(set(ev["kinds"]) | {it["kind"]})
                if it["ukey"]:
                    ev["keys"] = sorted(set(ev["keys"]) | {it["ukey"]})
                ev["cats"] = sorted(set(ev["cats"]) | categories(it["title"], it.get("items", [])))
                ev["count"] = int(ev["count"] or 1) + 1
                ev["forced"] = 1 if (forced or ev.get("forced")) else 0
            ev["level"] = self._final_level(ev)
            it["event_id"] = ev["id"]
            self.store.add_item(it)
            self.store.save_event(ev)
            if new and it["kind"] != "filing" and self.classifier.available(et_date_str()):
                asyncio.create_task(self._classify(ev["id"], it))
            else:
                self._maybe_notify(ev)
        if items:
            self.app.hub.publish("news_changed", {"ts": time.time()})
            market = getattr(self.app, "market", None)
            if market:
                market.publish_soon()  # catalyst headlines on the sector leaders

    @staticmethod
    def _final_level(ev: dict) -> str:
        lvl = (ev.get("l2") or {}).get("importance") or ev.get("l1_level") or "低"
        if ev.get("forced"):
            lvl = "高"
        return lvl

    async def _classify(self, event_id: str, item: dict) -> None:
        async with self._l2_lock:  # one call at a time keeps the daily cap exact
            res = await self.classifier.classify(item, _fmt_et(item["published"]) or "", self.watch_symbols(), et_date_str())
        ev = self.store.get_event(event_id)
        if ev is None:
            return
        if res:
            ev["l2"] = res
            ev["level"] = self._final_level(ev)
            self.store.save_event(ev)
            self._ok("layer2")
            self.app.hub.publish("news_changed", {"ts": time.time()})
        elif self.classifier.last_error:
            self._err("layer2", self.classifier.last_error)
        self._maybe_notify(ev)

    def _maybe_notify(self, ev: dict) -> None:
        lvl = LEVEL_NUM.get(ev["level"], 1)
        threshold = 2 if self.c("notify_medium", False) else 3
        if lvl < threshold or lvl <= int(ev.get("notified") or 0):
            return
        if time.time() - (ev.get("published") or 0) > float(self.c("notify_max_age_min", 120)) * 60:
            return  # backlog from the first poll: stored, shown, not notified
        ev["notified"] = lvl
        self.store.save_event(ev)
        view = self.event_view(ev, self.store.event_items([ev["id"]]).get(ev["id"], []))
        log.info("NEWS %s %s %s", ev["level"], ",".join(ev["symbols"]), ev["title"])
        self.app.hub.publish("news_alert", view)

    # ------------------------------------------------------------------ views
    def event_view(self, ev: dict, items: list[dict]) -> dict:
        l2 = ev.get("l2") or {}
        precision = "second" if any(i.get("time_precision") == "second" for i in items) else (
            items[0].get("time_precision") if items else "second")
        links, seen = [], set()
        for i in items:
            if i["url"] and i["url"] not in seen:
                seen.add(i["url"])
                links.append({"source": i["source_name"], "title": i["title"], "url": i["url"], "lang": i.get("lang")})
        sec_items = sorted({x for i in items if i.get("kind") == "filing" for x in (i.get("items") or [])})
        return {"id": ev["id"], "published": ev["published"], "time_et": _fmt_et(ev["published"]),
                "time_precision": precision, "symbols": ev["symbols"], "title": ev["title"], "level": ev["level"],
                "l1_level": ev.get("l1_level"), "reasons": ev.get("reasons") or [],
                "summary_zh": l2.get("summary_zh"), "l2_reason": l2.get("reason"), "l2_level": l2.get("importance"),
                "sources": ev.get("sources") or [], "count": ev.get("count") or 1, "links": links,
                "forms": sorted({i.get("form") for i in items if i.get("form")}),
                "sec_items": [{"item": x, "zh": SEC_ITEM_ZH.get(x, "")} for x in sec_items]}

    def query(self, symbol: str | None = None, levels: list[str] | None = None, source: str | None = None,
              limit: int = 150, hours: float = 72) -> list[dict]:
        evs = self.store.list_events(symbol=symbol or None, levels=levels or None, source=source or None,
                                     since=time.time() - hours * 3600, limit=limit)
        items = self.store.event_items([e["id"] for e in evs])
        return [self.event_view(e, items.get(e["id"], [])) for e in evs]

    def high_age_min(self, symbol: str, within_min: float = 1440) -> float | None:
        ts = self.store.latest_level_time(symbol, "高", time.time() - within_min * 60)
        return round((time.time() - ts) / 60, 2) if ts else None

    def catalyst(self, symbol: str) -> dict | None:
        evs = self.store.list_events(symbol=symbol, since=time.time() - 86400, limit=1)
        if not evs:
            return None
        items = self.store.event_items([evs[0]["id"]]).get(evs[0]["id"], [])
        link = next((i for i in items if i.get("url")), None)
        return {"title": evs[0]["title"], "level": evs[0]["level"], "time_et": _fmt_et(evs[0]["published"]),
                "url": link["url"] if link else None, "source": link["source_name"] if link else None}

    def sources_view(self) -> list[dict]:
        out = []
        for name, label in SOURCE_LABELS.items():
            if name == "mock" and not self.app.mock:
                continue
            st = self.status.get(name, {})
            note = None
            if name == "sec" and self.src("sec").get("enabled", True) and not self.app.mock:
                if not EMAIL_RE.match(str(self.c("contact_email", "") or "")):
                    note = "需要先在设置中填写联系邮箱"
            if self.app.mock and name in ("moomoo_search", "sec"):
                note = "模拟模式不访问网络"
            if name == "layer2":
                note = self.classifier.status(et_date_str())["notice"]
            out.append({"name": name, "label": label, "enabled": self.enabled(name), "last_ok": st.get("last_ok"),
                        "last_error": st.get("last_error"), "error_at": st.get("error_at"), "note": note})
        return out

    # --------------------------------------------------------------- settings
    def settings(self) -> dict:
        kw = self.c("keywords", {}) or {}
        return {
            "sources": {k: {"enabled": self.src(k).get("enabled", True), **self.src(k)}
                        for k in ("moomoo_search", "moomoo_api", "sec", "earnings")},
            "contact_email": self.c("contact_email", "") or "",
            "keywords": {"high": list(kw.get("high") or DEFAULT_HIGH_KEYWORDS),
                         "medium": list(kw.get("medium") or DEFAULT_MEDIUM_KEYWORDS)},
            "extra_keywords": self.extra_keywords(),
            "symbol_aliases": self.aliases(),
            "search_keywords": {s: self.search_keyword(s)[0] for s in self.watch_symbols()},
            "notify_medium": bool(self.c("notify_medium", False)),
            "layer2": self.classifier.status(et_date_str()),
            "retention_days": self.c("retention_days", 30), "mock": self.app.mock,
        }

    def update_settings(self, body: dict) -> dict:
        cfg = self.app.cfg
        if "sources" in body:
            for name, val in (body["sources"] or {}).items():
                if name not in ("moomoo_search", "moomoo_api", "sec", "earnings") or "enabled" not in (val or {}):
                    continue
                cfg.set(("news", "sources", name, "enabled"), bool(val["enabled"]), save=False)
        if "contact_email" in body:
            email = str(body["contact_email"] or "").strip()
            if email and not EMAIL_RE.match(email):
                raise UserError("邮箱格式不正确")
            cfg.set(("news", "contact_email"), email, save=False)
        if "keywords" in body:
            kw = body["keywords"] or {}
            high = [str(x).strip() for x in kw.get("high", []) if str(x).strip()]
            medium = [str(x).strip() for x in kw.get("medium", []) if str(x).strip()]
            if not high:
                raise UserError("高重要性关键词不能为空")
            cfg.set(("news", "keywords"), {"high": high, "medium": medium}, save=False)
        if "extra_keywords" in body:
            extra = [str(x).strip() for x in body["extra_keywords"] or [] if str(x).strip()]
            if len(extra) > 20:
                raise UserError("关注关键词最多 20 个")
            cfg.set(("news", "extra_keywords"), extra, save=False)
        if "symbol_aliases" in body:
            aliases = {}
            for k, v in (body["symbol_aliases"] or {}).items():
                code = normalize_code(k)
                if code.startswith("US.") and str(v).strip():
                    aliases[code] = str(v).strip()[:60]
            cfg.set(("news", "symbol_aliases"), aliases, save=False)
        if "notify_medium" in body:
            cfg.set(("news", "notify_medium"), bool(body["notify_medium"]), save=False)
        if "layer2_enabled" in body:
            cfg.set(("news", "layer2", "enabled"), bool(body["layer2_enabled"]), save=False)
        cfg.save()
        self.reload_keywords()
        self.app.hub.publish("news_settings", self.settings())
        return self.settings()
