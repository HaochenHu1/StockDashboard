"""Market overview page: sector change, capital flow, sector leaders, benchmark strip.

Data method (verified against OpenD 10.12.7208, see README):
* Sector universe: get_plate_list(US, INDUSTRY | CONCEPT) + get_plate_stock per plate,
  fetched once per US trading day (10 / 30 s each, spread by the rate limiter) and cached.
* Sector change: get_market_snapshot accepts plate codes and returns the plate level and
  previous close plus rise / fall / flat counts, so the change comes from moomoo directly.
  If a plate has no snapshot value, the market-cap weighted change of its constituents is used.
* Capital flow: get_capital_flow rejects plate codes ("只支持正股、窝轮和基金"), so a sector's
  net inflow is the sum of the intraday net inflow (cumulative, regular hours only) of its
  top 5 size-filtered constituents by dollar volume. One round queries at most 60 codes (the
  stalest first) and takes 1-2 minutes; rounds run back to back until every top-5 stock has
  today's value, then every 5 minutes. Coverage (their share of the sector's eligible dollar
  volume) is reported. An ETF proxy mode is available.
* Leaders: top gainer per sector among constituents with market cap >= 2B USD and dollar
  volume >= 20M USD (both editable).
This page uses snapshots only (no subscription quota), runs at the lowest priority and is
capped at half of the snapshot budget through the rate limiter's consumer cap.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..alerts import normalize_code
from ..errors import UserError
from ..gateway.base import OpenDError
from ..ratelimit import Priority
from ..timeutil import ET, et_date_str, et_str, us_session
from .calc import (_num, china_codes, is_china_stocks_plate, passes_size_filter, pct_change, plate_allowed, sector_leader,
                   top_bottom, up_down_counts, weighted_change)

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)
CONSUMER = "market"
PLATE_SETS = {"INDUSTRY": "行业板块", "CONCEPT": "概念板块"}
DEFAULT_ETF_PROXIES = [
    {"code": "US.XLK", "name": "科技"}, {"code": "US.XLF", "name": "金融"}, {"code": "US.XLE", "name": "能源"},
    {"code": "US.XLV", "name": "医疗保健"}, {"code": "US.XLY", "name": "可选消费"}, {"code": "US.XLP", "name": "必需消费"},
    {"code": "US.XLI", "name": "工业"}, {"code": "US.XLU", "name": "公用事业"}, {"code": "US.XLB", "name": "原材料"},
    {"code": "US.XLRE", "name": "房地产"}, {"code": "US.XLC", "name": "通信服务"}, {"code": "US.SMH", "name": "半导体"},
]


def _quantiles(vals: list[float]) -> dict | None:
    """min / p10 / p25 / median of complete top-5 coverages (used to choose min_flow_coverage_pct)."""
    if not vals:
        return None
    v = sorted(vals)
    q = lambda f: v[min(len(v) - 1, int(f * (len(v) - 1) + 0.5))]   # noqa: E731
    return {"n": len(v), "min": v[0], "p10": q(0.10), "p25": q(0.25), "median": q(0.5)}


class MarketService:
    def __init__(self, app: "AppState", cache_dir: Path):
        self.app = app
        self.dir = cache_dir / "market"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.plates: dict[str, list[dict]] = {}
        self.members: dict[str, list[str]] = {}
        self.names: dict[str, str] = {}
        self.plates_day: dict[str, str] = {}
        self._partial: dict[str, tuple[str, set[str]]] = {}   # set -> (day, plate codes already loaded)
        self.loading: dict | None = None
        self.stocks: dict[str, dict] = {}
        self.sectors: dict[str, dict] = {}
        self.flows: dict[str, dict] = {}
        self.benchmarks: list[dict] = []
        self.updated: dict[str, str | None] = {"sectors": None, "flows": None, "benchmarks": None}
        self.errors: dict[str, str | None] = {"sectors": None, "flows": None, "plates": None}
        self._full_pass_day: str | None = None
        self._flow_cursor = 0
        self.flow_round: dict | None = None   # last round: codes, seconds, universe size, still missing
        self._force = asyncio.Event()
        self._flow_force = False
        self.bad_codes: set[str] = set()   # codes OpenD refuses to quote (e.g. OTC): skipped everywhere
        self.sec_china: set[str] = set()   # SEC registrants based in mainland China / Hong Kong / Macau
        self.sec_taiwan: set[str] = set()  # SEC registrants based in Taiwan: not China-concept stocks here
        self.sec_china_day: str | None = None
        self.sec_china_error: str | None = None
        self._sec_china_next = 0.0          # monotonic time of the next attempt after a failure
        self._tasks: list[asyncio.Task] = []
        self._load_cache()
        self._load_bad_codes()
        self._load_sec_china()

    # ------------------------------------------------------------------ config
    def c(self, key: str, default: Any) -> Any:
        v = self.app.cfg.get("market", key, default=default)
        return default if v is None else v

    @property
    def plate_set(self) -> str:
        st = str(self.c("plate_set", "INDUSTRY")).upper()
        return st if st in PLATE_SETS else "INDUSTRY"

    @property
    def min_cap(self) -> float:
        return float(self.c("min_market_cap_usd", 2e9))

    @property
    def min_dv(self) -> float:
        return float(self.c("min_dollar_volume_usd", 2e7))

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> None:
        for st in PLATE_SETS:
            f = self.dir / f"plates_{st}.json"
            if f.exists():
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    self.plates[st] = d["plates"]
                    self.members.update(d["members"])
                    self.names.update(d.get("names", {}))
                    if d.get("complete", True):
                        self.plates_day[st] = d["day"]
                    else:  # interrupted load: resume from where it stopped
                        self._partial[st] = (d["day"], set(d["members"]))
                except (OSError, ValueError, KeyError):
                    log.warning("market plate cache %s unreadable", f)
        f = self.dir / "market_state.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                self.sectors = d.get("sectors", {})
                self.flows = d.get("flows", {})
                self.benchmarks = d.get("benchmarks", [])
                self.updated.update(d.get("updated", {}))
                self.stocks = d.get("stocks", {})
            except (OSError, ValueError):
                log.warning("market state cache unreadable")

    def _load_sec_china(self) -> None:
        f = self.dir / "china_sec.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                self.sec_china, self.sec_china_day = set(d.get("codes") or []), d.get("day")
                self.sec_taiwan = set(d.get("taiwan") or [])
                if "taiwan" not in d:
                    self.sec_china_day = None   # cache from an older version: refresh now
                    self.sec_china -= {"US.TSM", "US.UMC", "US.ASX"}   # until the refresh lists Taiwan
            except (OSError, ValueError):
                pass

    async def ensure_sec_china(self) -> None:
        """Weekly: China / Hong Kong based companies from SEC EDGAR (needs the SEC contact email)."""
        if not self.us_only or getattr(self.app, "mock", False):
            return
        days = max(1, int(self.c("china_sec_refresh_days", 7)))
        today = et_date_str()
        if self.sec_china_day and (dt.date.fromisoformat(today) - dt.date.fromisoformat(self.sec_china_day)).days < days:
            return
        news = getattr(self.app, "news", None)
        if not news or not getattr(news, "sec", None) or time.monotonic() < self._sec_china_next:
            return
        try:
            ua = news.sec_ua()
        except Exception as e:  # no contact email yet
            self.sec_china_error = str(e)
            return
        try:
            found = await news.sec.located_tickers(ua)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            self.sec_china_error = (f"SEC 中概股名单更新失败（{f'HTTP {status}' if status else type(e).__name__}），"
                                    f"{'已沿用上次名单' if self.sec_china else '暂时只用 moomoo 中概股板块'}，1 小时后重试")
            self._sec_china_next = time.monotonic() + 3600
            log.warning("market: SEC China company list failed: %s", e)
            return
        failed = int(found.get("failed") or 0)
        taiwan = {f"US.{t}" for t in found.get("taiwan") or ()}
        china = {f"US.{t}" for t in found.get("china") or ()}
        if failed:   # some queries failed: keep what the earlier list had, retry the full list in an hour
            taiwan |= self.sec_taiwan
            china = (china | self.sec_china) - taiwan
            self._sec_china_next = time.monotonic() + 3600
            self.sec_china_error = f"SEC 中概股名单有 {failed} 项查询失败，已保留上次名单，1 小时后重试"
            log.warning("market: SEC China company list incomplete (%d queries failed)", failed)
        if china:
            self.sec_china, self.sec_taiwan = china, taiwan
            if not failed:
                self.sec_china_day, self.sec_china_error = today, None
            self._write("china_sec.json", {"day": None if failed else today, "codes": sorted(self.sec_china),
                                           "taiwan": sorted(self.sec_taiwan)})
            log.info("market: %d SEC registrants based in mainland China / Hong Kong / Macau are excluded "
                     "(%d Taiwan-based kept)", len(self.sec_china), len(self.sec_taiwan))
            self._force.set()

    def _load_bad_codes(self) -> None:
        f = self.dir / "unsupported_codes.json"
        if f.exists():
            try:
                self.bad_codes = set(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    async def snapshot_robust(self, codes: list[str], priority: int) -> list[dict]:
        """Snapshot in batches; OpenD rejects a whole batch when one code is not quotable
        (e.g. "暂不提供美股 OTC 市场行情 AAGC"). Drop the named code (or bisect when no code is
        named), remember it, and retry the rest."""
        codes = [c for c in dict.fromkeys(codes) if c not in self.bad_codes]
        out: list[dict] = []
        size = max(1, int(self.app.cfg.get("rate_limits", "get_market_snapshot", default={}).get("max_codes", 400)))
        stack = [codes[i:i + size] for i in range(0, len(codes), size)]
        new_bad: set[str] = set()
        while stack:
            batch = [c for c in stack.pop() if c not in new_bad]
            if not batch:
                continue
            try:
                out.extend(await self.app.client.snapshot(batch, priority, CONSUMER))
            except OpenDError as e:
                named = [c for c in batch if re.search(r"(?<![A-Z0-9.])" + re.escape(c.split(".", 1)[1]) + r"(?![A-Z0-9])", e.msg)]
                if named:
                    new_bad.update(named)
                    stack.append([c for c in batch if c not in named])
                elif len(batch) == 1:
                    new_bad.add(batch[0])
                else:
                    mid = len(batch) // 2
                    stack.extend([batch[:mid], batch[mid:]])
        if new_bad:
            self.bad_codes |= new_bad
            self._write("unsupported_codes.json", sorted(self.bad_codes))
            log.info("market: %d codes are not quotable (e.g. OTC) and are skipped: %s", len(new_bad),
                     ", ".join(sorted(new_bad)[:10]))
        return out

    def _write(self, name: str, data: Any) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / name)

    def _save_state(self) -> None:
        keep = {c: self.stocks[c] for c in self.leader_codes() if c in self.stocks}
        self._write("market_state.json", {"sectors": self.sectors, "flows": self.flows, "benchmarks": self.benchmarks,
                                          "updated": self.updated, "stocks": keep})

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        # one fresh calculation per start, also outside regular hours: the cached state may come
        # from an older version or different settings (US-only rules, size filter, plate set)
        self._force.set()
        self._flow_force = True
        self._tasks = [asyncio.create_task(self._plates_loop()), asyncio.create_task(self._refresh_loop()),
                       asyncio.create_task(self._flow_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    def _connected(self) -> bool:
        return self.app.gw.connection_state() == "connected"

    # ---------------------------------------------------------- US stocks only
    @property
    def us_only(self) -> bool:
        return bool(self.c("us_only", True))

    def manual_china(self) -> set[str]:
        return {normalize_code(c) for c in self.c("exclude_codes", []) or []}

    def manual_keep(self) -> set[str]:
        return {normalize_code(c) for c in self.c("keep_codes", []) or []}

    def china(self) -> set[str]:
        """China-concept stocks: members of moomoo's China-concept plates (both sets) + SEC registrants based in
        mainland China / Hong Kong / Macau + market.exclude_codes. Taiwan-based companies (TSM, UMC, ASX, which
        moomoo lists under 热门中概股) and market.keep_codes stay in, unless listed in exclude_codes."""
        if not self.us_only:
            return set()
        plates = [p for ps in self.plates.values() for p in ps]
        manual = self.manual_china()
        return (china_codes(plates, self.members, self.sec_china) - self.sec_taiwan - self.manual_keep()) | manual

    def us_members(self, plate_code: str, china: set[str] | None = None) -> list[str]:
        china = self.china() if china is None else china
        return [c for c in self.members.get(plate_code, []) if c not in china and c not in self.bad_codes]

    def visible_plates(self, china: set[str] | None = None) -> list[dict]:
        plates = self.plates.get(self.plate_set, [])
        if not self.us_only:
            return plates
        china = self.china() if china is None else china
        return [p for p in plates if plate_allowed(p["name"], self.members.get(p["code"]), china)]

    def _affected(self, plate_code: str, china: set[str]) -> bool:
        """A US plate that contains China-concept stocks: its change is recomputed from US stocks only."""
        return bool(china) and any(c in china for c in self.members.get(plate_code, []))

    async def ensure_china_set(self) -> None:
        """Load the constituents of China-themed plates first, so they can be excluded everywhere."""
        if not self.us_only:
            return
        for st in PLATE_SETS:
            for p in self.plates.get(st, []):
                if is_china_stocks_plate(p["name"]) and p["code"] not in self.members:
                    recs = await self.app.client.plate_stock(p["code"], Priority.BACKGROUND, CONSUMER)
                    self.members[p["code"]] = [r["code"] for r in recs if r.get("code")]

    # ---------------------------------------------------------- plates (daily)
    async def _plates_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                if self._connected():
                    await self.ensure_plate_lists()   # both lists first (2 calls) so switching sets never waits
                    await self.ensure_china_set()     # then the China-themed plates (excluded everywhere)
                    await self.ensure_sec_china()     # and China / Hong Kong based companies from SEC (weekly)
                    today = et_date_str()
                    order = [self.plate_set] + [s for s in PLATE_SETS if s != self.plate_set]
                    for st in order:
                        if self.plates_day.get(st) != today:
                            await self._load_plates(st)
            except OpenDError as e:
                self.errors["plates"] = e.msg
                log.warning("loading plates failed: %s", e)
            except Exception:
                log.exception("plates loop failed")
            await asyncio.sleep(60)

    async def ensure_plate_lists(self) -> None:
        for st in PLATE_SETS:
            if self.plates.get(st):
                continue
            lst = await self.app.client.plate_list(st, Priority.BACKGROUND, CONSUMER)
            self.plates[st] = [{"code": p["code"], "name": p.get("plate_name") or p["code"]} for p in lst if p.get("code")]
            if st == self.plate_set:
                self._force.set()

    async def _load_plates(self, st: str) -> None:
        client = self.app.client
        lst = await client.plate_list(st, Priority.BACKGROUND, CONSUMER)
        plates = [{"code": p["code"], "name": p.get("plate_name") or p["code"]} for p in lst if p.get("code")]
        self.plates[st] = plates   # plate-level numbers show at once; cached constituents stay until replaced
        self._force.set()
        today = et_date_str()
        day, done = self._partial.get(st, (today, set()))
        if day != today:
            done = set()
        self.loading = {"set": st, "done": len(done), "total": len(plates)}
        members: dict[str, list[str]] = {c: self.members.get(c, []) for c in done}
        for p in plates:
            if p["code"] in done:
                continue
            try:
                recs = await client.plate_stock(p["code"], Priority.BACKGROUND, CONSUMER)
            except OpenDError as e:
                log.warning("plate_stock %s failed: %s", p["code"], e)
                continue
            codes = []
            for r in recs:
                c = r.get("code")
                if c and str(r.get("stock_type", "STOCK")) in ("STOCK", "ETF", "None"):
                    codes.append(c)
                    if r.get("stock_name"):
                        self.names[c] = r["stock_name"]
            members[p["code"]] = codes
            self.members[p["code"]] = codes          # leaders appear while the rest is still loading
            done.add(p["code"])
            self.loading = {"set": st, "done": len(done), "total": len(plates)}
            if len(done) % 10 == 0:
                self._write(f"plates_{st}.json", {"day": today, "plates": plates, "members": members, "complete": False,
                                                  "names": {c: self.names.get(c) for ms in members.values()
                                                            for c in ms if self.names.get(c)}})
                self._partial[st] = (today, set(done))
                self.publish()
            if len(done) % 30 == 0 and st == self.plate_set:
                self._full_pass_day = None
                self._force.set()
        self.plates[st] = plates
        self.members.update(members)
        self.plates_day[st] = et_date_str()
        self.loading = None
        self.errors["plates"] = None
        names = {c: self.names.get(c) for ms in members.values() for c in ms if self.names.get(c)}
        self._write(f"plates_{st}.json", {"day": self.plates_day[st], "plates": plates, "members": members,
                                          "names": names, "complete": True})
        self._partial.pop(st, None)
        log.info("market: loaded %d %s plates", len(plates), st)
        self._full_pass_day = None
        self._force.set()
        self.publish()

    # --------------------------------------------------------- 60 s refresh
    async def _refresh_loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            # Always consume the flag. Leaving it set (e.g. the chosen plate set has no plate list
            # yet) made the wait below return at once and spin the loop without ever yielding.
            forced = self._force.is_set()
            self._force.clear()
            try:
                due = self._connected() and (us_session() == "RTH" or not self.updated.get("sectors") or forced)
                if due and self.plates.get(self.plate_set):
                    await self.refresh()
                elif forced:
                    self.publish()  # plate list for this set not in yet: show the loading state
            except OpenDError as e:
                self.errors["sectors"] = e.msg
                log.warning("market refresh failed: %s", e)
            except Exception:
                log.exception("market refresh crashed")
            try:
                await asyncio.wait_for(self._force.wait(), timeout=float(self.c("refresh_sec", 60)))
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.5)  # debounce bursts of refresh requests; guarantees the loop yields

    async def refresh(self) -> None:
        client = self.app.client
        st = self.plate_set
        china = self.china()
        plates = self.visible_plates(china)
        if not plates:
            return
        # 1. plate snapshots: sector change straight from moomoo (plates without China-concept stocks)
        for r in await client.snapshot([p["code"] for p in plates], Priority.BACKGROUND, CONSUMER):
            code = r.get("code")
            name = next((p["name"] for p in plates if p["code"] == code), r.get("name") or code)
            sec = self.sectors.setdefault(code, {"code": code})
            sec.update({"name": name, "set": st, "turnover": _num(r.get("turnover"))})
            if self._affected(code, china):
                continue  # moomoo's plate number includes China-concept stocks: recomputed below from US stocks
            chg = pct_change(r.get("last_price"), r.get("prev_close_price"))
            sec.update({"change_pct": round(chg, 3) if chg is not None else None,
                        "method": "plate_snapshot" if chg is not None else None})
            if r.get("plate_valid"):
                sec.update({"up": r.get("plate_raise_count"), "down": r.get("plate_fall_count"),
                            "flat": r.get("plate_equal_count")})
        self.updated["sectors"] = et_str()
        self.publish()  # sector changes first; leaders follow once constituents are in
        # 2. benchmark strip
        await self.refresh_benchmarks()
        # 3. constituents for leaders (and the weighted fallback)
        universe = sorted({c for p in plates for c in self.us_members(p["code"], china)})
        affected = {c for p in plates if self._affected(p["code"], china) for c in self.us_members(p["code"], china)}
        today = et_date_str()
        if self._full_pass_day != today:
            targets = universe  # learn market caps once per day
        else:
            # big names for leaders, plus every US stock of plates whose change is recomputed
            targets = [c for c in universe if c not in self.stocks or c in affected or
                       (_num(self.stocks[c].get("market_cap")) or 0) >= self.min_cap * 0.8]
        if targets:
            for r in await self.snapshot_robust(targets, Priority.BACKGROUND):
                self._ingest_stock(r)
            if self._full_pass_day != today and len(targets) == len(universe):
                self._full_pass_day = today
        self._compute_sectors()
        self.updated["sectors"] = et_str()
        self.errors["sectors"] = None
        self._save_state()
        self.publish()

    async def refresh_benchmarks(self) -> None:
        codes = [normalize_code(c) for c in self.c("benchmarks", ["US.SPY", "US.QQQ", "US.TQQQ", "US.DIA", "US.SOXX"])]
        if not codes:
            self.benchmarks = []
            return
        out = []
        recs = {r["code"]: r for r in await self.app.client.snapshot(codes, Priority.BACKGROUND, CONSUMER)}
        for c in codes:
            r = recs.get(c)
            if not r:
                continue
            chg = pct_change(r.get("last_price"), r.get("prev_close_price"))
            out.append({"code": c, "name": r.get("name"), "last": _num(r.get("last_price")),
                        "change_pct": round(chg, 3) if chg is not None else None})
        self.benchmarks = out
        self.updated["benchmarks"] = et_str()

    def _ingest_stock(self, r: dict) -> None:
        code = r.get("code")
        if not code:
            return
        chg = pct_change(r.get("last_price"), r.get("prev_close_price"))
        self.stocks[code] = {"code": code, "name": r.get("name") or self.names.get(code) or code,
                             "last": _num(r.get("last_price")), "change_pct": round(chg, 3) if chg is not None else None,
                             "market_cap": _num(r.get("total_market_val")), "dollar_volume": _num(r.get("turnover"))}

    def _compute_sectors(self) -> None:
        china = self.china()
        for p in self.visible_plates(china):
            sec = self.sectors.setdefault(p["code"], {"code": p["code"], "name": p["name"], "set": self.plate_set})
            cons = [self.stocks[c] for c in self.us_members(p["code"], china) if c in self.stocks]
            n_cn = sum(1 for c in self.members.get(p["code"], []) if c in china)
            sec["excluded"] = n_cn
            if n_cn:
                w = weighted_change(cons)  # US stocks only
                sec.update({"change_pct": round(w, 3) if w is not None else None,
                            "method": "us_only" if w is not None else None})
                sec["up"], sec["down"], sec["flat"] = up_down_counts(cons)
            elif sec.get("change_pct") is None and cons:
                w = weighted_change(cons)
                sec.update({"change_pct": round(w, 3) if w is not None else None,
                            "method": "weighted" if w is not None else None})
            if sec.get("up") is None and cons:
                sec["up"], sec["down"], sec["flat"] = up_down_counts(cons)
            leader = sector_leader(cons, self.min_cap, self.min_dv)
            sec["leader"] = dict(leader) if leader else None
            sec["constituents"] = len(self.us_members(p["code"], china))

    # ------------------------------------------------------------ capital flow
    async def _flow_loop(self) -> None:
        await asyncio.sleep(5)
        catch_up = False
        while True:
            pending, catch_up = catch_up, False
            try:
                forced, self._flow_force = self._flow_force, False
                # outside regular hours a cycle runs only when forced (start, plate-set switch) and then
                # keeps catching up until every top-5 stock has a value
                if self._connected() and (us_session() == "RTH" or not self.updated.get("flows") or forced or pending) \
                        and self.plates.get(self.plate_set) and self.stocks:
                    await self.refresh_flows()
                    # back to back while top-5 stocks still lack a value, as long as the round got answers
                    # (the universe can grow mid-way, e.g. right after a plate-set switch)
                    catch_up = bool(self.flow_round and self.flow_round.get("missing") and self.flow_round.get("ok")) \
                        and self.c("capital_flow_method", "constituents") != "etf_proxy"
            except OpenDError as e:
                self.errors["flows"] = e.msg
                log.warning("capital flow refresh failed: %s", e)
            except Exception:
                log.exception("capital flow loop crashed")
            end = time.monotonic() + (3.0 if catch_up else float(self.c("capital_flow_refresh_sec", 300)))
            while time.monotonic() < end and not self._flow_force:   # a plate-set switch starts a cycle early
                await asyncio.sleep(1 if catch_up else 2)

    # Each sector's flow = its top N size-filtered US constituents by dollar volume (N = 5).
    def _flow_top(self, plate_code: str, china: set[str]) -> tuple[list[dict], list[dict]]:
        """(eligible constituents, top N of them by dollar volume)."""
        n = max(1, int(self.c("capital_flow_top_per_sector", 5)))
        elig = [self.stocks[c] for c in self.us_members(plate_code, china) if c in self.stocks
                and passes_size_filter(self.stocks[c], self.min_cap, self.min_dv)]
        elig.sort(key=lambda x: x.get("dollar_volume") or 0, reverse=True)
        return elig, elig[:n]

    def _flow_universe(self) -> list[dict]:
        china = self.china()
        uni: dict[str, dict] = {}
        for p in self.visible_plates(china):
            for st in self._flow_top(p["code"], china)[1]:
                uni[st["code"]] = st
        return sorted(uni.values(), key=lambda x: x.get("dollar_volume") or 0, reverse=True)

    @staticmethod
    def _flow_day(f: dict | None) -> str | None:
        """Trading day a stored flow value belongs to (from the data's own time)."""
        if not f or f.get("in_flow") is None:
            return None
        t = str(f.get("time") or "")
        if len(t) >= 10:
            return t[:10]
        return dt.datetime.fromtimestamp(float(f.get("fetched") or 0), ET).date().isoformat() if f.get("fetched") else None

    def _latest_flow_day(self) -> str | None:
        days = [d for d in (self._flow_day(f) for f in self.flows.values()) if d]
        return max(days) if days else None

    def _flow_current(self, code: str, latest: str | None) -> bool:
        d = self._flow_day(self.flows.get(code))
        return d is not None and d == latest

    def _flow_missing(self) -> list[str]:
        latest = self._latest_flow_day()
        return [st["code"] for st in self._flow_universe() if not self._flow_current(st["code"], latest)]

    def _flow_targets(self) -> list[str]:
        """At most capital_flow_calls_per_cycle (60) codes: never / not today measured first, then the
        stalest, larger dollar volume first on ties."""
        uni = self._flow_universe()
        budget = max(1, int(self.c("capital_flow_calls_per_cycle", 60)))
        latest = self._latest_flow_day()

        def key(st: dict) -> tuple:
            f = self.flows.get(st["code"]) or {}
            return (1 if self._flow_current(st["code"], latest) else 0, float(f.get("fetched") or 0),
                    -(st.get("dollar_volume") or 0))
        return [st["code"] for st in sorted(uni, key=key)[:budget]]

    async def refresh_flows(self) -> None:
        client = self.app.client
        if self.c("capital_flow_method", "constituents") == "etf_proxy":
            targets = [p["code"] for p in self.c("etf_proxies", DEFAULT_ETF_PROXIES)]
        else:
            targets = self._flow_targets()
        t0, ok = time.monotonic(), 0
        for code in targets:
            try:
                rows = await client.capital_flow(code, "INTRADAY", Priority.BACKGROUND, CONSUMER)
            except OpenDError as e:
                log.debug("capital flow %s failed: %s", code, e)
                continue
            if rows:
                last = rows[-1]
                self.flows[code] = {"in_flow": _num(last.get("in_flow")), "time": last.get("capital_flow_item_time"),
                                    "fetched": time.time()}
                ok += 1
        secs = round(time.monotonic() - t0, 1)
        uni = len(self._flow_universe()) if self.c("capital_flow_method", "constituents") != "etf_proxy" else len(targets)
        missing = len(self._flow_missing()) if self.c("capital_flow_method", "constituents") != "etf_proxy" else 0
        self.flow_round = {"codes": len(targets), "ok": ok, "seconds": secs, "universe": uni, "missing": missing,
                           "finished": et_str()}
        log.info("capital flow round: %d codes (%d ok) in %.1f s; top-5 universe %d, still missing %d",
                 len(targets), ok, secs, uni, missing)
        self.updated["flows"] = et_str()
        self.errors["flows"] = None
        self._save_state()
        self.publish()

    def _sector_flows(self) -> list[dict]:
        if self.c("capital_flow_method", "constituents") == "etf_proxy":
            rows = []
            for p in self.c("etf_proxies", DEFAULT_ETF_PROXIES):
                f = self.flows.get(p["code"])
                s = self.stocks.get(p["code"]) or {}
                if f and f.get("in_flow") is not None:
                    rows.append({"code": p["code"], "name": f"{p['name']}（{p['code'][3:]} ETF 代理）",
                                 "flow": f["in_flow"], "change_pct": s.get("change_pct"), "coverage": None})
            return rows
        rows, self._flow_below, self._flow_cov_all = [], 0, []
        china = self.china()
        latest = self._latest_flow_day()
        threshold = float(self.c("min_flow_coverage_pct", 20))
        for p in self.visible_plates(china):
            elig, top = self._flow_top(p["code"], china)
            covered = [st for st in top if self._flow_current(st["code"], latest)]
            if not covered:
                continue
            total_dv = sum(st.get("dollar_volume") or 0 for st in elig)
            cov_dv = sum(st.get("dollar_volume") or 0 for st in covered)
            coverage = round(cov_dv / total_dv * 100, 1) if total_dv else None
            if coverage is not None and len(covered) == len(top):
                self._flow_cov_all.append(coverage)
            if coverage is not None and coverage < threshold:
                self._flow_below += 1
                continue  # the measured stocks are too small a part of the sector: the number would mislead
            sec = self.sectors.get(p["code"], {})
            rows.append({"code": p["code"], "name": p["name"],
                         "flow": sum(self.flows[st["code"]]["in_flow"] for st in covered),
                         "change_pct": sec.get("change_pct"), "covered": len(covered), "top": len(top),
                         "eligible": len(elig), "coverage": coverage})
        return rows

    # ------------------------------------------------------------------ views
    def leader_codes(self) -> list[str]:
        out = []
        for p in self.visible_plates():
            ld = (self.sectors.get(p["code"]) or {}).get("leader")
            if ld:
                out.append(ld["code"])
        return out

    def state(self) -> dict:
        st = self.plate_set
        china = self.china()
        visible = self.visible_plates(china)
        hidden = [p["name"] for p in self.plates.get(st, []) if p not in visible]
        sectors = [dict(self.sectors[p["code"]]) for p in visible if p["code"] in self.sectors]
        for s in sectors:
            s.pop("constituents_list", None)
        n_sec = max(1, int(self.c("top_n_sectors", 5)))
        n_flow = max(1, int(self.c("top_n_flows", 10)))
        n_lead = max(1, int(self.c("top_n_leaders", 5)))
        gainers, losers = top_bottom(sectors, "change_pct", n_sec)
        flow_rows = self._sector_flows()
        inflow, outflow = top_bottom(flow_rows, "flow", n_flow)
        leaders = [{"sector": s["name"], "sector_code": s["code"], **s["leader"]} for s in sectors if s.get("leader")]
        leaders.sort(key=lambda x: x.get("change_pct") or -1e9, reverse=True)
        leaders = leaders[:n_lead]
        news = getattr(self.app, "news", None)
        for ld in leaders:  # catalyst lookups only for the rows shown
            ld["catalyst"] = news.catalyst(ld["code"]) if news else None
        methods = {s.get("method") for s in sectors if s.get("method")} - {"us_only"}
        cov = [r["coverage"] for r in flow_rows if r.get("coverage") is not None]
        usage = self.app.limiter.usage()
        budget = {ep: (usage.get(ep) or {}).get("consumers", {}).get(CONSUMER)
                  for ep in ("get_market_snapshot", "get_capital_flow")}
        return {
            "plate_set": st, "plate_sets": PLATE_SETS, "loading": self.loading, "session": us_session(),
            "updated": self.updated, "errors": self.errors, "plates_day": self.plates_day.get(st),
            "size_filter": {"min_market_cap_usd": self.min_cap, "min_dollar_volume_usd": self.min_dv},
            "benchmarks": self.benchmarks,
            "benchmark_codes": self.c("benchmarks", ["US.SPY", "US.QQQ", "US.TQQQ", "US.DIA", "US.SOXX"]),
            "change_method": ("plate_snapshot" if methods == {"plate_snapshot"} else
                              "weighted" if methods == {"weighted"} else "mixed" if methods else None),
            "flow_method": self.c("capital_flow_method", "constituents"),
            "flow_rules": {"top_per_sector": max(1, int(self.c("capital_flow_top_per_sector", 5))),
                           "calls_per_cycle": max(1, int(self.c("capital_flow_calls_per_cycle", 60))),
                           "refresh_sec": float(self.c("capital_flow_refresh_sec", 300)),
                           "min_coverage_pct": float(self.c("min_flow_coverage_pct", 20)),
                           "listed": len(flow_rows), "below_threshold": getattr(self, "_flow_below", 0),
                           "coverage_stats": _quantiles(getattr(self, "_flow_cov_all", [])),
                           "round": self.flow_round},
            "flow_coverage": round(sum(cov) / len(cov), 1) if cov else None,
            "sector_count": len(sectors), "gainers": gainers, "losers": losers,
            "inflow": inflow, "outflow": outflow, "leaders": leaders, "budget": budget,
            "top_n": {"sectors": n_sec, "flows": n_flow, "leaders": n_lead},
            "us_only": {"enabled": self.us_only, "china_codes": len(china), "hidden_plates": hidden,
                        "china_sources": {"plates": len(china_codes(
                            [p for ps in self.plates.values() for p in ps], self.members)),
                            "sec": len(self.sec_china), "manual": len(self.manual_china()),
                            "taiwan_kept": sorted(c for c in china_codes(
                                [p for ps in self.plates.values() for p in ps], self.members) if c in self.sec_taiwan),
                            "sec_day": self.sec_china_day, "sec_error": self.sec_china_error},
                        "recomputed": sum(1 for s in sectors if s.get("method") == "us_only")},
        }

    def publish(self) -> None:
        self.app.hub.publish("market", self.state())

    def publish_soon(self) -> None:
        """Debounced publish, e.g. after news arrived (catalyst headlines)."""
        if getattr(self, "_pub_pending", False):
            return
        self._pub_pending = True

        async def later() -> None:
            await asyncio.sleep(3)
            self._pub_pending = False
            self.publish()
        asyncio.create_task(later())

    # ---------------------------------------------------------------- actions
    async def sector_detail(self, code: str) -> dict:
        if self.members.get(code) is None:
            raise UserError("该板块的成分股尚未加载，请稍后再试")
        members = self.us_members(code)   # China-concept stocks are never shown
        recs = await self.snapshot_robust(members, Priority.NORMAL) if members else []
        for r in recs:
            self._ingest_stock(r)
        rows = [dict(self.stocks[c], passes=passes_size_filter(self.stocks[c], self.min_cap, self.min_dv))
                for c in members if c in self.stocks]
        rows.sort(key=lambda s: s.get("change_pct") if s.get("change_pct") is not None else -1e9, reverse=True)
        return {"code": code, "name": (self.sectors.get(code) or {}).get("name"), "constituents": rows,
                "updated": et_str()}

    def set_plate_set(self, st: str) -> dict:
        st = str(st).upper()
        if st not in PLATE_SETS:
            raise UserError("板块类型只能是行业板块或概念板块")
        self.app.cfg.set(("market", "plate_set"), st)
        self._full_pass_day = None
        self._flow_force = True
        if not self.plates.get(st) and self._connected():
            asyncio.create_task(self._fetch_lists_now())
        self._force.set()
        self.publish()
        return self.state()

    async def _fetch_lists_now(self) -> None:
        try:
            await self.ensure_plate_lists()
        except OpenDError as e:
            self.errors["plates"] = e.msg

    def set_size_filter(self, min_cap: Any, min_dv: Any) -> dict:
        try:
            cap, dv = float(min_cap), float(min_dv)
        except (TypeError, ValueError):
            raise UserError("市值与成交额门槛必须是数字") from None
        if cap < 0 or dv < 0:
            raise UserError("门槛不能为负数")
        self.app.cfg.set(("market", "min_market_cap_usd"), cap, save=False)
        self.app.cfg.set(("market", "min_dollar_volume_usd"), dv)
        self._compute_sectors()
        self.publish()
        return self.state()

    async def set_benchmarks(self, codes: list[str]) -> dict:
        from ..services.stocks import validate_stock_code
        clean = []
        for c in codes:
            if str(c).strip():
                clean.append(validate_stock_code(c))
        if len(clean) > 20:
            raise UserError("基准最多 20 个")
        self.app.cfg.set(("market", "benchmarks"), clean)
        if self._connected():
            await self.refresh_benchmarks()
        self.publish()
        return self.state()

    def request_refresh(self) -> dict:
        self._force.set()
        return {"message": "已安排刷新"}
