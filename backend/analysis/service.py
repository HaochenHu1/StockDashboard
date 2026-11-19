"""个股分析 service: POST /api/stock-analysis.

Flow per request (prompt 1.2): quote + fundamentals -> option chain (<= 7 days) -> news (moomoo + web
search inside the model call) -> analysis (Claude, JSON only) -> trade plan checks in code -> result.

* Results are cached for 30 minutes per symbol (memory + data/<mode>/analysis/*.json); "重新生成" skips it.
* Progress of the five steps is pushed over the WebSocket (`analysis_progress`).
* One job per symbol: a second request for the same symbol joins the running job.
* US stocks only: China-concept stocks are rejected and removed from candidates and related companies.
* Mock mode never calls the API; it builds a clearly labelled simulated analysis (see mock.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..alerts import normalize_code
from ..errors import UserError
from ..gateway.base import OpenDError, is_option_code
from ..news.sources import is_non_us
from ..ratelimit import Priority
from ..services.stocks import validate_stock_code
from ..timeutil import et_date_str, now_et
from . import data as D
from .llm import AnalysisError, AnalysisLLM, key_present
from .mock import mock_analysis, mock_delay_steps
from .validate import check_trade_plans, normalize

if TYPE_CHECKING:
    from ..app_state import AppState

log = logging.getLogger(__name__)

STEPS = [("quote", "获取行情"), ("chain", "获取期权链"), ("news", "检索新闻"), ("analysis", "生成分析"),
         ("validate", "校验交易方案")]
US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z])?$")
SKIP_SEC_TYPES = {"DRVT", "WARRANT", "BOND", "FUTURE", "IDX", "INDEX", "PLATE", "PLATESET"}


class StockAnalysisService:
    def __init__(self, app: "AppState", data_dir: Path):
        self.app = app
        self.dir = data_dir / "analysis"
        self.llm = AnalysisLLM(self.c)
        self.simulate = app.mock          # mock mode: simulated analysis, no API call
        self.cache: dict[str, dict] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.progress: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self._sem: asyncio.Semaphore | None = None
        self._search_cache: dict[str, tuple[float, list[dict]]] = {}

    def c(self, key: str, default: Any = None) -> Any:
        return self.app.cfg.get("stock_analysis", key, default=default)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._sem = asyncio.Semaphore(max(1, int(self.c("max_concurrent", 2))))
        self.dir.mkdir(parents=True, exist_ok=True)
        for p in self.dir.glob("US_*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                self.cache[r["code"]] = r
            except (OSError, ValueError, KeyError):
                continue

    async def stop(self) -> None:
        for t in self.jobs.values():
            t.cancel()

    # ---------------------------------------------------------------- state
    def _usage_path(self) -> Path:
        return self.dir / "usage.json"

    def used_today(self) -> int:
        try:
            u = json.loads(self._usage_path().read_text(encoding="utf-8"))
            return int(u.get("count", 0)) if u.get("date") == et_date_str() else 0
        except (OSError, ValueError):
            return 0

    def _count_use(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._usage_path().write_text(json.dumps({"date": et_date_str(), "count": self.used_today() + 1}),
                                      encoding="utf-8")

    def enabled(self) -> bool:
        """The tab is switched off unless config.yaml sets stock_analysis.enabled: true."""
        return bool(self.c("enabled", False))

    def settings(self) -> dict:
        if not self.enabled():
            return {"enabled": False}
        return {"enabled": True, "mode": "mock" if self.simulate else "live", "key_present": key_present(),
                "model": self.c("model", "claude-opus-5-5"), "effort": self.c("effort", "high"),
                "cache_minutes": self.c("cache_minutes", 30), "daily_cap": self.c("daily_cap", 50),
                "used_today": self.used_today(),
                "rules": {"max_option_days": self.c("max_option_days", 7), "min_open_interest": self.c("min_open_interest", 500),
                          "max_spread_pct": self.c("max_spread_pct", 10)}}

    def resolve(self, raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            raise UserError("请输入股票代码，例如 NVDA")
        code = validate_stock_code(normalize_code(text))
        if code in self.app.market.china():
            raise UserError(f"看板只覆盖美股，不含中概股：{code} 属于中概股。"
                            "如需分析，请把它写进 config.yaml 的 market.keep_codes")
        return code

    def _fresh(self, code: str) -> dict | None:
        r = self.cache.get(code)
        if r and time.time() - float(r.get("generated_ts", 0)) < float(self.c("cache_minutes", 30)) * 60:
            return r
        return None

    # ------------------------------------------------------------------- api
    async def analyze(self, raw: str, refresh: bool = False) -> dict:
        code = self.resolve(raw)
        task = self.jobs.get(code)
        if task is None or task.done():
            if not refresh:
                hit = self._fresh(code)
                if hit:
                    return {**hit, "cached": True}
            if not self.simulate:
                if not key_present():
                    raise UserError("未配置 Anthropic API 密钥：请在项目目录的 .env 中填写 ANTHROPIC_API_KEY，然后重启看板")
                cap = int(self.c("daily_cap", 50))
                if self.used_today() >= cap:
                    raise UserError(f"今日个股分析已达上限 {cap} 次（stock_analysis.daily_cap），请明天再试或修改配置")
            self.errors.pop(code, None)
            task = asyncio.create_task(self._run(code))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())   # errors are kept in self.errors
            self.jobs[code] = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=float(self.c("request_wait_sec", 240)))
        except asyncio.TimeoutError:
            return {"status": "running", "code": code, "ticker": code.split(".", 1)[1], "progress": self.progress.get(code)}

    def job(self, raw: str) -> dict:
        code = normalize_code(raw)
        task = self.jobs.get(code)
        if task is not None and not task.done():
            return {"status": "running", "code": code, "progress": self.progress.get(code)}
        if code in self.errors:
            return {"status": "error", "code": code, "detail": self.errors[code], "progress": self.progress.get(code)}
        if task is not None and code in self.cache:          # job of this session just finished
            return {**self.cache[code], "status": "done", "cached": False}
        hit = self._fresh(code)
        if hit:
            return {**hit, "status": "done", "cached": True}
        return {"status": "none", "code": code}

    # -------------------------------------------------------------- progress
    def _new_progress(self, code: str) -> dict:
        p = {"code": code, "ticker": code.split(".", 1)[1], "state": "running", "started_at": now_et().isoformat(),
             "steps": [{"key": k, "label": lab, "state": "waiting", "detail": ""} for k, lab in STEPS], "error": None}
        self.progress[code] = p
        self._publish(p)
        return p

    def _step(self, p: dict, key: str, state: str, detail: str | None = None) -> None:
        for s in p["steps"]:
            if s["key"] == key:
                s["state"] = state
                if detail is not None:
                    s["detail"] = detail
        self._publish(p)

    def _publish(self, p: dict) -> None:
        self.app.hub.publish("analysis_progress", p)

    # ------------------------------------------------------------------ run
    async def _run(self, code: str) -> dict:
        p = self._new_progress(code)
        assert self._sem is not None
        try:
            async with self._sem:
                result = await self._pipeline(code, p)
        except asyncio.CancelledError:
            raise
        except UserError as e:
            self._fail(p, code, str(e))
            raise
        except Exception:
            log.exception("stock analysis %s crashed", code)
            msg = "分析失败：服务端内部错误，详见 logs\\runtime.log"
            self._fail(p, code, msg)
            raise UserError(msg) from None
        p["state"] = "done"
        self._publish(p)
        return result

    def _fail(self, p: dict, code: str, msg: str) -> None:
        for s in p["steps"]:
            if s["state"] == "running":
                s["state"], s["detail"] = "error", msg
        p["state"], p["error"] = "error", msg
        self.errors[code] = msg
        self._publish(p)

    async def _pipeline(self, code: str, p: dict) -> dict:
        mock = self.simulate
        delays = mock_delay_steps() if mock else [0, 0, 0]

        # 1. quote + fundamentals
        self._step(p, "quote", "running", "moomoo OpenD 快照与财务数据")
        market = await D.collect_market(self.app, code)
        px = market["price"]
        n_gap = len(market["gaps"])
        self._step(p, "quote", "done", f"价格 {px['value']:.2f}（{px['source'].split('（')[-1].rstrip('）')}）"
                   + (f"；{n_gap} 项基本面数据未取得" if n_gap else "") if px.get("value") else "价格未取得")
        await asyncio.sleep(delays[0])

        # 2. option chain (<= 7 calendar days)
        self._step(p, "chain", "running", f"到期日 ≤ {self.c('max_option_days', 7)} 个自然日")
        chain_error = None
        try:
            chain = await D.collect_chain(self.app, code, px.get("value"), int(self.c("max_option_days", 7)),
                                          float(self.c("chain_strike_window_pct", 20)),
                                          int(self.c("chain_max_strikes_each_side", 30)))
            n_c = sum(len(e["contracts"]) for e in chain["expirations"])
            self._step(p, "chain", "done", chain.get("note") or
                       f"{len(chain['expirations'])} 个到期日（{', '.join(e['expiry'] for e in chain['expirations'])}），{n_c} 个合约")
        except UserError as e:
            chain_error = str(e)
            chain = {"underlying": code, "spot": px.get("value"), "expirations": [], "error": chain_error}
            self._step(p, "chain", "error", f"{chain_error}；期权方案将全部无法通过校验")
        await asyncio.sleep(delays[1])

        # 3. news (moomoo) + web search inside the model call
        self._step(p, "news", "running", "moomoo 资讯与公告")
        news, news_gaps = await D.collect_news(self.app, code, market["name"])
        gaps = market["gaps"] + news_gaps
        searches = {"n": 0, "writing": False}

        def on_event(kind: str) -> None:
            if kind == "search":
                searches["n"] += 1
                self._step(p, "news", "running", f"moomoo 资讯 {len(news)} 条；网络搜索第 {searches['n']} 次")
            elif kind == "text" and searches["n"] and not searches["writing"]:
                searches["writing"] = True
                self._step(p, "news", "done", f"moomoo 资讯 {len(news)} 条；网络搜索 {searches['n']} 次")
                self._step(p, "analysis", "running", f"{self.c('model', 'claude-opus-5-5')} 正在撰写分析")

        # 4. analysis
        if mock:
            await asyncio.sleep(delays[2])
            self._step(p, "news", "done", f"moomoo 资讯 {len(news)} 条（模拟模式不做网络搜索）")
            self._step(p, "analysis", "running", "模拟模式：生成模拟分析，不调用 API")
            peers = self._peers(code)
            llm = {"data": mock_analysis(market, chain, news, peers), "searched_urls": None, "searches": 0,
                   "usage": {}, "model": "mock", "attempts": 1}
            await asyncio.sleep(delays[2])
        else:
            self._step(p, "analysis", "running", f"{self.c('model', 'claude-opus-5-5')}：检索新闻并生成分析，通常需要 1 至 4 分钟")
            payload = self._payload(code, market, chain, news, gaps)
            llm = await self.llm.run(payload, on_event)
            self._count_use()
            if p["steps"][2]["state"] != "done":
                self._step(p, "news", "done", f"moomoo 资讯 {len(news)} 条；网络搜索 {llm['searches']} 次")
        self._step(p, "analysis", "done", f"模型 {llm['model']}" + ("（JSON 解析失败后重试 1 次）" if llm["attempts"] > 1 else ""))

        # 5. checks in code
        self._step(p, "validate", "running", "到期日、期权链、未平仓量、价差，并重算盈亏")
        result = self._assemble(code, market, chain, chain_error, llm, gaps, p)
        kept, removed = len(result["analysis"]["trade_plans"]), len(result["validation"]["removed"])
        self._step(p, "validate", "done", f"保留 {kept} 个方案" + (f"，删除 {removed} 个" if removed else ""))
        result["progress"] = p
        self.cache[code] = result
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f"{code.replace('.', '_')}.tmp"
            tmp.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self.dir / f"{code.replace('.', '_')}.json")
        except OSError:
            log.exception("writing analysis cache failed")
        return {**result, "cached": False}

    # --------------------------------------------------------------- helpers
    def _peers(self, code: str) -> list[tuple[str, str]]:
        m = self.app.market
        china = m.china()
        out: list[tuple[str, str]] = []
        for pc, members in m.members.items():
            if code in members:
                for c in members:
                    if c != code and c not in china and (c, m.names.get(c, c)) not in out:
                        out.append((c, m.names.get(c) or c.split(".", 1)[1]))
        return out[:8]

    def _payload(self, code: str, market: dict, chain: dict, news: list[dict], gaps: list[str]) -> dict:
        now = now_et()
        max_days = int(self.c("max_option_days", 7))
        return {
            "task": "按 system prompt 的规则，对下面这只美股做全面分析，只返回 JSON。",
            "ticker": code.split(".", 1)[1], "moomoo_code": code, "company_name": market["name"],
            "now_us_eastern": now.isoformat(), "today_us_eastern": now.date().isoformat(),
            "constraints": [
                f"期权方案只能使用 option_chain 中列出的合约；到期日不得晚于 {chain.get('latest_allowed_expiry') or '（无可用期权）'}"
                f"（今天起 {max_days} 个自然日内）。",
                f"服务端会删除未平仓量低于 {self.c('min_open_interest', 500)} 张、或买卖价差超过中间价 {self.c('max_spread_pct', 10)}% 的期权腿所在方案，选合约时请避开。",
                "option_chain 为空或没有合适合约时，只给正股方案或不给方案。",
                "关联公司只列美国上市的股票（可含在美上市的台湾公司），不列中概股、A 股、港股和其他非美上市公司。",
                "新闻只列与这只美股直接相关的条目。",
                "price 字段请照抄 market_data.price。",
                "data_gaps 列出的是取不到的数据，相应字段写「未取得」，不要用记忆或估算填充。",
            ],
            "market_data": {"price": market["price"], "snapshot": market["quote"], "company": market["company"]},
            "fundamentals_data": {"financial_statements": market["financials"], "valuation": market["valuation"],
                                  "analyst_consensus": market["analyst_consensus"],
                                  "earnings_price_moves_last_8": market["earnings_price_moves"],
                                  "implied_vs_historical_volatility": market["volatility"],
                                  "source": "moomoo OpenD F10 数据"},
            "option_chain": D.chain_for_model(chain),
            "moomoo_news": news,
            "data_gaps": gaps,
        }

    def _assemble(self, code: str, market: dict, chain: dict, chain_error: str | None, llm: dict,
                  gaps: list[str], p: dict) -> dict:
        a = normalize(llm["data"] or {})
        china = self.app.market.china()
        removed_related = []
        keep_related = []
        for r in a["related_companies"]:
            t = r["ticker"]
            if not t or not US_TICKER_RE.match(t) or f"US.{t}" in china or f"US.{t}" == code:
                removed_related.append(f"{r['name'] or t}（{t or '无代码'}）")
            else:
                keep_related.append(r)
        a["related_companies"] = keep_related
        n_news = len(a["news"]) + len(a["gossip"])
        a["news"] = [n for n in a["news"] if not is_non_us(n["title"])]
        a["gossip"] = [n for n in a["gossip"] if not is_non_us(n["title"])]
        n_news -= len(a["news"]) + len(a["gossip"])
        urls = llm.get("searched_urls")
        if urls is not None:
            for item in a["news"] + a["gossip"]:
                for s in item["sources"]:
                    s["in_search"] = bool(s["url"]) and s["url"] in urls
        today = now_et().date()
        spot = market["price"].get("value")
        kept, removed = check_trade_plans(a["trade_plans"], a["conclusion"]["action"], chain, today, spot,
                                          int(self.c("max_option_days", 7)), int(self.c("min_open_interest", 500)),
                                          float(self.c("max_spread_pct", 10)))
        a["trade_plans"] = kept
        warnings = []
        g = a["fundamentals"]["earnings_quality_grade"]
        if g in ("C", "D") and a["conclusion"]["action"] == "买入":
            warnings.append(f"财报可信度为 {g}，按分析规则不应给出看多动作，但模型给出了“买入”，请谨慎对待")
        if a["conclusion"]["action"] == "买入" and a["conclusion"]["direction_7d"] == "偏空":
            warnings.append("投资动作为“买入”，但未来 7 个交易日方向为“偏空”，两者不一致")
        now = now_et()
        sources = a["sources"]
        sources.insert(0, {"name": "moomoo OpenD：行情快照、财务数据（F10）、期权链与期权报价", "url": "https://openapi.moomoo.com/",
                           "accessed_at": market["price"].get("as_of") or now.isoformat()})
        a["price"] = market["price"]
        a["ticker"] = code.split(".", 1)[1]
        a["generated_at"] = now.isoformat()
        return {
            "status": "done", "ticker": code.split(".", 1)[1], "code": code, "name": market["name"],
            "generated_at": now.isoformat(), "generated_ts": time.time(),
            "mode": "mock" if self.simulate else "live", "model": llm["model"],
            "price": market["price"], "company": market["company"],
            "snapshot": {k: market["quote"].get(k) for k in ("pe_ttm_ratio", "pb_ratio", "highest52weeks_price",
                                                               "lowest52weeks_price", "dividend_ratio_ttm") if market["quote"].get(k) is not None},
            "analysis": a,
            "validation": {"removed": removed, "warnings": warnings, "chain_error": chain_error,
                           "chain_expiries": [{"expiry": e["expiry"], "dte": e["dte"], "contracts": len(e["contracts"]),
                                               "atm": e["atm"]} for e in chain.get("expirations") or []],
                           "latest_allowed_expiry": chain.get("latest_allowed_expiry"),
                           "rules": self.settings()["rules"]},
            "filtered": {"related_removed": removed_related, "news_removed": n_news},
            "data_gaps": gaps, "searches": llm["searches"], "usage": llm["usage"], "attempts": llm["attempts"],
        }

    # ---------------------------------------------------------------- search
    async def search(self, q: str) -> list[dict]:
        q = str(q or "").strip()
        if not q:
            return []
        china = self.app.market.china()
        names: dict[str, str] = {}
        names.update({c: n for c, n in self.app.market.names.items() if n})
        for w in self.app.stocks.watchlist:
            names.setdefault(w["code"], w["code"].split(".", 1)[1])
        for c, d in self.app.store.data.items():
            if not is_option_code(c) and d.get("name"):
                names[c] = d["name"]
        Q = q.upper().removeprefix("US.")
        ql = q.lower()
        scored = []
        for c, n in names.items():
            if not c.startswith("US.") or c in china or is_option_code(c):
                continue
            t = c[3:]
            nl = (n or "").lower()
            s = 0 if t == Q else 1 if t.startswith(Q) else 2 if nl.startswith(ql) else 3 if ql in nl else None
            if s is not None:
                scored.append((s, len(t), {"code": c, "ticker": t, "name": n, "source": "local"}))
        scored.sort(key=lambda x: (x[0], x[1]))
        out = [x[2] for x in scored[:8]]
        if len(out) < 5 and self.app.gw.connection_state() == "connected":
            for r in await self._remote_search(q):
                if r["code"] not in china and all(r["code"] != o["code"] for o in out):
                    out.append(r)
        return out[:8]

    async def _remote_search(self, q: str) -> list[dict]:
        key = q.lower()
        hit = self._search_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            rows = await asyncio.wait_for(self.app.client.search_quote(q, 10, Priority.NORMAL), timeout=4)
        except (asyncio.TimeoutError, OpenDError):
            return []
        out = []
        for r in rows or []:
            market = str(r.get("market") or "").upper()
            code = str(r.get("code") or "").upper()
            if not code or "US" not in market and not code.startswith("US."):
                continue
            if str(r.get("sec_type") or "").upper() in SKIP_SEC_TYPES:
                continue
            code = code if code.startswith("US.") else f"US.{code}"
            if code.startswith("US..") or is_option_code(code):
                continue
            out.append({"code": code, "ticker": code[3:], "name": r.get("name") or code[3:], "source": "moomoo"})
        self._search_cache[key] = (time.time(), out)
        return out


__all__ = ["StockAnalysisService", "AnalysisError"]
