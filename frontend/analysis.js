// 个股分析 tab. Talks only to the local backend: the Anthropic key stays in the server environment.
// Everything the model wrote is untrusted text and is escaped before it reaches the page.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => typeof v === "number" && isFinite(v);
const fx = (v, d = 2) => (isNum(v) ? v.toFixed(d) : "—");
const usd0 = (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : "—");
const MISSING = `<span class="faint">未取得</span>`;
const txt = (s) => (s && String(s).trim() && String(s).trim() !== "未取得" ? esc(s) : MISSING);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const SVG = (p, s = 16) => `<svg class="ic" viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const ICON = {
  waiting: SVG('<circle cx="12" cy="12" r="8"/>', 15),
  running: `<span class="spin" aria-hidden="true"></span>`,
  done: SVG('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 15),
  error: SVG('<path d="M18 6 6 18"/><path d="M6 6l12 12"/>', 15),
  chevron: SVG('<path d="M6 9l6 6 6-6"/>', 16),
  refresh: SVG('<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>', 14),
  ext: SVG('<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>', 13),
};
const CATS = [["filing", "公司公告与监管文件"], ["earnings", "财报与指引"], ["rating", "评级调整"],
  ["insider", "管理层与内部人士交易"], ["legal", "诉讼与监管"]];
const STATUS = { confirmed: ["已证实", "accent", "公司公告或监管文件"], reported: ["媒体报道", "", "可信媒体报道，公司未确认"],
  rumor: ["传闻", "outline", "未经证实的传闻"] };
const REL = { supplier: "上游供应商", customer: "下游客户", competitor: "直接竞争对手", substitute: "替代技术", partner: "合作伙伴" };
const IMPACT = { positive: ["利好", "up"], negative: ["利空", "down"], neutral: ["中性", "flat"] };
const ACT_CLS = { 买入: "up", 卖出: "down", 观望: "flat" };
const DIR_CLS = { 偏多: "up", 偏空: "down", 方向不明: "flat" };
const fmtET = new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtLocal = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const whenET = (iso) => { const d = new Date(iso); return isNaN(d) ? "—" : `美东 ${fmtET.format(d)}`; };
const whenLocal = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : `本地 ${fmtLocal.format(d)}`; };
const cap = (v) => (!isNum(v) ? null : v >= 1e12 ? `${(v / 1e12).toFixed(2)} 万亿美元` : v >= 1e8 ? `${(v / 1e8).toFixed(1)} 亿美元` : `${usd0(v)} 美元`);
const signedUsd = (v) => (!isNum(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`);

let ctx = { api: null, toast: () => {} };
const A = { restored: false, code: null, result: null, progress: null, busy: false, cands: [], active: -1, settings: null,
  searchTimer: null, searchSeq: 0, pollTimer: null };

// ─────────────────────────────────────────────────────────────── public
export function initAnalysis(c) {
  ctx = c;
  const form = $("#anForm"), input = $("#anInput");
  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("input", () => { clearTimeout(A.searchTimer); A.searchTimer = setTimeout(search, 220); });
  input.addEventListener("keydown", onKey);
  input.addEventListener("blur", () => setTimeout(closeList, 150));
  input.addEventListener("focus", () => { if (A.cands.length && input.value.trim()) openList(); });
  $("#anList").addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (!li) return;
    e.preventDefault();
    pick(+li.dataset.i);
  });
  $("#anMeta").addEventListener("click", (e) => { if (e.target.closest("#anRegen") && A.code) run(A.code, true); });
  const root = $("#anResult");
  root.addEventListener("input", (e) => { const t = e.target; if (t.dataset.calc != null) { store.set("anLoss", t.value); calc(t); } });
  root.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d.matches || !d.matches("details[data-sec]")) return;
    const closed = new Set(store.get("anClosed", "").split(",").filter(Boolean));
    if (d.open) closed.delete(d.dataset.sec); else closed.add(d.dataset.sec);
    store.set("anClosed", [...closed].join(","));
  }, true);
  setInterval(tickElapsed, 1000);
}

export function setAnalysisSettings(s) {
  A.settings = s || null;
  if (!s || !s.enabled) return;   // feature switched off (stock_analysis.enabled): no requests at all
  renderSub();
  if (!A.restored) {
    A.restored = true;
    const last = store.get("anLast", "");
    if (last) restore(last);
  }
}

export function onAnalysisProgress(p) {
  if (!p || (A.code && p.code !== A.code)) return;
  A.progress = p;
  renderProgress();
}

// ─────────────────────────────────────────────────────────────── search box
async function search() {
  const q = $("#anInput").value.trim();
  const seq = ++A.searchSeq;
  if (!q) { A.cands = []; closeList(); return; }
  try {
    const r = await ctx.api("GET", `/api/stock-analysis/search?q=${encodeURIComponent(q)}`);
    if (seq !== A.searchSeq) return;
    A.cands = r.candidates || [];
    A.active = A.cands.length ? 0 : -1;
    renderList();
  } catch { /* the box still works with a plain ticker */ }
}
function renderList() {
  const ul = $("#anList");
  if (!A.cands.length) { closeList(); return; }
  ul.innerHTML = A.cands.map((c, i) => `<li role="option" id="anOpt${i}" data-i="${i}" aria-selected="${i === A.active}" class="${i === A.active ? "on" : ""}">
      <span class="code">${esc(c.ticker)}</span><span class="nm">${esc(c.name || "")}</span></li>`).join("");
  openList();
}
function openList() { $("#anList").hidden = false; $("#anInput").setAttribute("aria-expanded", "true");
  $("#anInput").setAttribute("aria-activedescendant", A.active >= 0 ? `anOpt${A.active}` : ""); }
function closeList() { $("#anList").hidden = true; $("#anInput").setAttribute("aria-expanded", "false"); }
function onKey(e) {
  const open = !$("#anList").hidden && A.cands.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!A.cands.length) return;
    e.preventDefault();
    A.active = (A.active + (e.key === "ArrowDown" ? 1 : -1) + A.cands.length) % A.cands.length;
    renderList();
  } else if (e.key === "Escape" && open) {
    e.stopPropagation();
    closeList();
  }
}
function pick(i) {
  const c = A.cands[i];
  if (!c) return;
  $("#anInput").value = c.ticker;
  A.cands = [c]; A.active = 0;
  closeList();
}
function submit() {
  clearTimeout(A.searchTimer);
  A.searchSeq++;                       // a search still in flight must not reopen the list after Enter
  const raw = $("#anInput").value.trim();
  if (!raw) { ctx.toast("error", "请输入股票代码或公司名，例如 NVDA"); return; }
  const open = !$("#anList").hidden;
  let pickC = open && A.active >= 0 ? A.cands[A.active] : null;
  const asTicker = /^(US\.)?[A-Za-z][A-Za-z0-9.\-]{0,9}$/.test(raw);
  if (!pickC && !asTicker && A.cands.length) pickC = A.cands[0];     // a company name: take the best candidate
  if (!pickC && asTicker) pickC = A.cands.find((c) => c.ticker === raw.toUpperCase().replace(/^US\./, "")) || null;
  const ticker = pickC ? pickC.ticker : raw;
  if (pickC) $("#anInput").value = pickC.ticker;
  closeList();
  run(ticker, false);
}

// ─────────────────────────────────────────────────────────────── run
async function run(ticker, refresh) {
  if (A.busy) { ctx.toast("info", "正在生成分析，请稍候"); return; }
  const code = "US." + String(ticker).toUpperCase().replace(/^US\./, "");
  A.busy = true;
  A.code = code;
  A.progress = { code, ticker: code.slice(3), state: "running", started_at: new Date().toISOString(),
    steps: ["获取行情", "获取期权链", "检索新闻", "生成分析", "校验交易方案"].map((label) => ({ label, state: "waiting", detail: "" })) };
  if (!refresh) { A.result = null; renderResult(); }
  renderProgress(); renderMeta(); setBusy(true);
  try {
    let r = await ctx.api("POST", "/api/stock-analysis", { ticker, refresh: !!refresh });
    while (r && r.status === "running") {                     // long run: the server keeps working, poll the job
      if (r.progress) { A.progress = r.progress; renderProgress(); }
      await new Promise((res) => setTimeout(res, 3000));
      r = await ctx.api("GET", `/api/stock-analysis/job/${encodeURIComponent(code)}`);
      if (r.status === "error") throw new Error(r.detail || "分析失败");
    }
    if (!r || r.status === "none") throw new Error("分析结果丢失，请重新生成");
    show(r);
    store.set("anLast", r.code);
  } catch (e) {
    if (A.progress) {
      A.progress.state = "error";
      for (const s of A.progress.steps) if (s.state === "running" || s.state === "waiting") { s.state = s.state === "running" ? "error" : "waiting"; }
      A.progress.error = e.message;
      renderProgress();
    }
    ctx.toast("error", e.message);
  } finally {
    A.busy = false;
    setBusy(false);
    renderMeta();
    ctx.api("GET", "/api/stock-analysis/settings").then(setAnalysisSettings).catch(() => {});
  }
}

async function restore(code) {
  try {
    const r = await ctx.api("GET", `/api/stock-analysis/job/${encodeURIComponent(code)}`);
    if (r.status === "done" && r.analysis && !A.result && !A.busy) {
      A.code = r.code;
      $("#anInput").value = r.ticker;
      show(r);
    } else if (r.status === "running" && !A.busy) {
      A.code = code; A.progress = r.progress; renderProgress();
    }
  } catch { /* nothing to restore */ }
}

function show(r) {
  A.result = r;
  A.code = r.code;
  if (r.progress) A.progress = r.progress;
  renderProgress(); renderMeta(); renderResult();
  if (r.cached) ctx.toast("info", `使用 ${whenET(r.generated_at)} 生成的缓存结果（${(A.settings && A.settings.cache_minutes) || 30} 分钟内有效）`);
}

function setBusy(b) {
  const btn = $("#anGo");
  btn.disabled = b;
  btn.textContent = b ? "生成中…" : "生成分析";
  const rg = $("#anRegen");
  if (rg) rg.disabled = b;
}

// ─────────────────────────────────────────────────────────────── header bits
function renderSub() {
  const s = A.settings, el = $("#anSub");
  if (!el || !s) return;
  if (s.mode === "mock") {
    el.innerHTML = `模拟模式：行情、财务与期权链为模拟数据，分析内容为模拟生成，<b>不调用 API</b>。交易方案仍由服务端代码校验`;
  } else {
    const key = s.key_present ? "" : ` · <span class="warn-inline">未配置 ANTHROPIC_API_KEY（在 .env 中填写后重启看板）</span>`;
    el.innerHTML = `行情、财务与期权链来自 moomoo OpenD；新闻由 ${esc(s.model)} 联网检索；交易方案由服务端代码校验 · 今日已用 ${s.used_today}/${s.daily_cap} 次${key}`;
  }
}

function renderMeta() {
  const r = A.result, el = $("#anMeta");
  if (!r) { el.innerHTML = ""; return; }
  const cacheTag = r.cached ? `<span class="tag" title="同一代码 ${(A.settings && A.settings.cache_minutes) || 30} 分钟内重复查询直接使用缓存">缓存结果</span>` : "";
  el.innerHTML = `<div class="an-when"><span>生成于 <b>${esc(whenET(r.generated_at))}</b></span><span class="muted">${esc(whenLocal(r.generated_at))}</span></div>
    ${cacheTag}<button class="btn small" id="anRegen" ${A.busy ? "disabled" : ""}>${ICON.refresh}重新生成</button>`;
}

function renderProgress() {
  const p = A.progress, el = $("#anProgress");
  if (!p) { el.hidden = true; return; }
  el.hidden = false;
  const state = { running: "分析中", done: "已完成", error: "未完成" }[p.state] || "";
  const started = Date.parse(p.started_at) || Date.now();
  el.innerHTML = `<div class="step-head"><span><b>${esc(p.ticker)}</b> · ${state}</span>
      <span class="muted num" ${p.state === "running" ? `data-elapsed="${started}"` : ""}>${p.state === "running" ? elapsed(started) : ""}</span></div>
    <ol class="steps">${p.steps.map((s) => `<li class="step ${esc(s.state)}"><span class="si">${ICON[s.state] || ICON.waiting}</span>
      <span class="sl">${esc(s.label)}</span><span class="sd">${esc(s.detail || "")}</span></li>`).join("")}</ol>
    ${p.error ? `<div class="step-err">${esc(p.error)}</div>` : ""}`;
}
function elapsed(t0) { const s = Math.max(0, Math.round((Date.now() - t0) / 1000)); return `已用时 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function tickElapsed() { for (const el of $$("[data-elapsed]")) el.textContent = elapsed(+el.dataset.elapsed); }

// ─────────────────────────────────────────────────────────────── result
function renderResult() {
  const root = $("#anResult"), r = A.result;
  if (!r) { root.innerHTML = ""; return; }
  const a = r.analysis;
  const closed = new Set(store.get("anClosed", "").split(",").filter(Boolean));
  const sec = (key, title, count, body) => `<details class="an-sec" data-sec="${key}" ${closed.has(key) ? "" : "open"}>
      <summary><h3>${title}</h3>${count != null ? `<span class="cnt">${count}</span>` : ""}<span class="chev">${ICON.chevron}</span></summary>
      <div class="an-body">${body}</div></details>`;
  const nNews = a.news.length, nPlans = a.trade_plans.length;
  root.innerHTML = [
    r.mode === "mock" ? `<div class="panel an-mock"><span class="tag dark">模拟模式</span><span>以下内容为模拟生成，用于测试界面，不是真实研究或投资建议。</span></div>` : "",
    sec("concl", "结论", null, conclHTML(r)),
    sec("company", "公司概览", null, companyHTML(r)),
    sec("fund", "基本面与财报质量", a.fundamentals.earnings_quality_grade ? `财报可信度 ${esc(a.fundamentals.earnings_quality_grade)}` : null, fundHTML(a)),
    sec("val", "估值与预期差", a.valuation.methods.length ? `${a.valuation.methods.length} 种方法` : null, valHTML(a, r)),
    sec("news", "新闻", nNews ? `${nNews} 条` : null, newsHTML(a)),
    sec("gossip", "花边新闻", a.gossip.length ? `${a.gossip.length} 条` : null, gossipHTML(a)),
    sec("related", "关联公司", a.related_companies.length ? `${a.related_companies.length} 家` : null, relatedHTML(a, r)),
    sec("plans", "交易方案", a.conclusion.action === "观望" ? "观望" : `${nPlans} 个`, plansHTML(r)),
    sec("watch", "监控清单与来源", null, watchHTML(a, r)),
  ].join("");
  const saved = store.get("anLoss", "");
  for (const inp of $$("[data-calc]", root)) { if (saved) inp.value = saved; calc(inp); }
}

function conclHTML(r) {
  const c = r.analysis.conclusion, g = r.analysis.fundamentals.earnings_quality_grade;
  const warn = (r.validation.warnings || []).map((w) => `<div class="an-warn">${esc(w)}</div>`).join("");
  return `<div class="panel concl">
    <div class="concl-top">
      <div class="concl-cell"><div class="k">投资动作</div><div class="act ${ACT_CLS[c.action] || ""}">${esc(c.action)}</div></div>
      <div class="concl-cell"><div class="k">未来 7 个交易日方向</div><div class="dirv ${DIR_CLS[c.direction_7d] || ""}">${esc(c.direction_7d)}</div></div>
      <div class="concl-cell"><div class="k">财报可信度</div><div class="dirv">${g ? esc(g) : MISSING}</div></div>
    </div>
    <div class="concl-body">
      <div><div class="k">核心理由</div>${c.reasons.length ? `<ol>${c.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : `<p>${MISSING}</p>`}</div>
      <div><div class="k">最大风险</div><p>${txt(c.top_risk)}</p></div>
    </div>${warn}</div>`;
}

function kv(k, v, sub = "") {
  return `<div class="kv"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
}
function companyHTML(r) {
  const co = r.company || {}, p = r.price || {}, sn = r.snapshot || {};
  const hl = isNum(sn.highest52weeks_price) && isNum(sn.lowest52weeks_price) ? `${fx(sn.lowest52weeks_price)} – ${fx(sn.highest52weeks_price)}` : MISSING;
  const gaps = (r.data_gaps || []);
  return `<div class="panel"><div class="kv-grid">
      ${kv("公司", `${esc(co.name || r.name || r.ticker)} <span class="muted">${esc(r.ticker)}</span>`)}
      ${kv("当前价格", isNum(p.value) ? `<span class="num">${fx(p.value)}</span> <span class="muted">美元</span>` : MISSING,
        `取价时间 ${esc(whenET(p.as_of))} · ${esc(p.source || "")}`)}
      ${kv("市值", cap(co.market_cap) ? esc(cap(co.market_cap)) : MISSING, "moomoo OpenD 快照")}
      ${kv("所属行业", (co.industry || []).length ? esc(co.industry.join("、")) : MISSING,
        (co.concepts || []).length ? `概念：${esc(co.concepts.slice(0, 4).join("、"))}` : "")}
      ${kv("市盈率 TTM", isNum(sn.pe_ttm_ratio) ? fx(sn.pe_ttm_ratio) : MISSING)}
      ${kv("52 周区间", hl)}
    </div>
    ${gaps.length ? `<div class="an-gaps"><span class="k">未取得的数据</span>${gaps.map((g) => `<span>${esc(g)}</span>`).join("")}</div>` : ""}</div>`;
}

function fundHTML(a) {
  const f = a.fundamentals;
  const rows = f.metrics.map((m) => `<tr><td>${esc(m.name)}</td><td class="r num">${txt(m.value)}</td><td>${txt(m.period)}</td><td class="muted wrap">${txt(m.source)}</td></tr>`).join("");
  return `<div class="panel">
    <div class="grade-row"><div class="grade ${f.earnings_quality_grade ? "g" + f.earnings_quality_grade : ""}">${esc(f.earnings_quality_grade || "—")}</div>
      <div><div class="k">财报可信度（A 至 D，按应计质量和收入确认风险评定）</div><p>${txt(f.notes)}</p></div></div>
    ${rows ? `<div class="scroll-x"><table class="grid"><thead><tr><th>指标</th><th class="r">数值</th><th>期间</th><th>来源</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">关键财务指标：未取得</div>`}</div>`;
}

function valHTML(a, r) {
  const px = r.price && r.price.value;
  const rows = a.valuation.methods.map((m) => {
    const rel = isNum(m.fair_value) && isNum(px) && px > 0 ? (m.fair_value / px - 1) * 100 : null;
    return `<tr><td>${esc(m.method)}</td><td class="r num">${isNum(m.fair_value) ? fx(m.fair_value) : MISSING}</td>
      <td class="r num ${rel == null ? "" : rel > 0 ? "up" : rel < 0 ? "down" : "flat"}">${rel == null ? "—" : (rel > 0 ? "+" : "") + rel.toFixed(1) + "%"}</td>
      <td class="wrap">${txt(m.assumptions)}</td></tr>`;
  }).join("");
  return `<div class="panel">${rows ? `<div class="scroll-x"><table class="grid"><thead><tr><th>估值方法</th><th class="r">公允价值</th><th class="r">相对现价</th><th>主要假设</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">估值结果：未取得</div>`}
    <div class="an-para"><div class="k">市场隐含预期与独立判断的差异</div><p>${txt(a.valuation.expectations_gap)}</p></div></div>`;
}

function srcLinks(sources) {
  if (!sources.length) return `<span class="faint">无来源链接</span>`;
  return sources.map((s) => {
    const mark = s.in_search === false ? ` <span class="unverified" title="这个链接没有出现在本次网络搜索的结果中，请自行核实">未在检索结果中</span>` : "";
    return s.url ? `<a class="lnk" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a>${mark}` : `<span>${esc(s.name)}</span>`;
  }).join("");
}
function itemHTML(n, withReaction) {
  const st = STATUS[n.status] || STATUS.rumor;
  const d = /^\d{4}-\d{2}-\d{2}$/.test(n.date) ? n.date : "";
  return `<div class="tl">
    <div class="when"><b>${d ? esc(d.slice(5)) : "—"}</b><span>${d ? esc(d.slice(0, 4)) : "日期未取得"}</span></div>
    <div class="body">
      <div class="meta"><span class="tag ${st[1]}" title="${esc(st[2])}">${st[0]}</span>
        ${withReaction && n.price_reaction ? `<span>股价反应：${esc(n.price_reaction)}</span>` : ""}</div>
      <span class="hl">${esc(n.title)}</span>
      ${n.summary ? `<div class="zh">${esc(n.summary)}</div>` : ""}
      <div class="links">${srcLinks(n.sources)}</div>
    </div></div>`;
}
function newsHTML(a) {
  return CATS.map(([k, label]) => {
    const items = a.news.filter((n) => n.category === k);
    return `<div class="sec-label an-label">${label}${items.length ? ` · ${items.length}` : ""}</div>
      <div class="panel">${items.length ? items.map((n) => itemHTML(n, true)).join("") : `<div class="empty">未取得</div>`}</div>`;
  }).join("");
}
function gossipHTML(a) {
  return `<p class="note an-note">高管个人言论与私人事件、社交媒体争议、名人合作、员工与公司文化事件、市场传闻</p>
    <div class="panel">${a.gossip.length ? a.gossip.map((n) => itemHTML(n, false)).join("") : `<div class="empty">未取得</div>`}</div>`;
}

function relatedHTML(a, r) {
  const rows = a.related_companies.map((c) => {
    const im = IMPACT[c.impact] || IMPACT.neutral;
    return `<tr><td><span class="code">${esc(c.ticker || "—")}</span> <span class="muted">${esc(c.name)}</span></td>
      <td>${esc(REL[c.relation] || c.relation)}</td><td class="wrap">${txt(c.evidence)}</td><td class="wrap">${txt(c.recent_event)}</td>
      <td class="${im[1]}">${im[0]}</td></tr>`;
  }).join("");
  const removed = (r.filtered && r.filtered.related_removed) || [];
  return `<div class="panel">${rows ? `<div class="scroll-x"><table class="grid an-rel"><thead><tr><th>公司与代码</th><th>关系类型</th><th>依据</th><th>近期事件</th><th>影响方向</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">关联公司：未取得</div>`}
    <div class="an-para"><div class="k">传导关系</div><p>${txt(a.transmission)}</p></div>
    ${removed.length ? `<div class="an-gaps"><span class="k">只看美股：已剔除</span><span>${esc(removed.join("、"))}</span></div>` : ""}</div>`;
}

// ─────────────────────────────────────────────────────────────── trade plans
function plansHTML(r) {
  const a = r.analysis, v = r.validation || {};
  if (a.conclusion.action === "观望") {
    return `<div class="panel"><div class="an-para"><div class="k">结论为“观望”，不提供交易方案</div>
      ${a.conclusion.reasons.length ? `<ol>${a.conclusion.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : `<p>${MISSING}</p>`}</div></div>`;
  }
  const rules = v.rules || {};
  const note = `<p class="note an-note">服务端已用代码校验：期权腿到期日 ≤ ${rules.max_option_days ?? 7} 个自然日（最晚 ${esc(v.latest_allowed_expiry || "—")}）、合约在期权链中、未平仓量 ≥ ${rules.min_open_interest ?? 500} 张、买卖价差 ≤ 中间价的 ${rules.max_spread_pct ?? 10}%；盈亏平衡点、最大收益与亏损、所需资金均按各腿中间价重新计算。</p>`;
  const cards = a.trade_plans.map((p, i) => planCard(p, i)).join("");
  const removed = (v.removed || []).length ? `<div class="panel an-removed"><div class="k">已删除 ${v.removed.length} 个未通过校验的方案</div>
      ${v.removed.map((x) => `<div class="rm"><b>${esc(x.structure)}</b>${x.reasons.map((y) => `<span>${esc(y)}</span>`).join("")}</div>`).join("")}</div>` : "";
  const chainErr = v.chain_error ? `<div class="an-warn">期权链未取得：${esc(v.chain_error)}</div>` : "";
  return note + chainErr + (cards ? `<div class="plan-grid">${cards}</div>` : `<div class="panel"><div class="empty">没有通过校验的交易方案</div></div>`) + removed;
}

function planCard(p, i) {
  const c = p.computed || {};
  const rows = [["下单方式", p.order], ["止盈条件", p.take_profit], ["止损条件", p.stop_loss], ["最晚离场时间", p.latest_exit], ["失效条件", p.invalidation]]
    .map(([k, v]) => `<div class="pr"><span class="k">${k}</span><span>${txt(v)}</span></div>`).join("");
  const risks = p.risks.length ? `<ul>${p.risks.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : MISSING;
  let body, metrics, calcLabel;
  if (p.type === "option") {
    const dte = Math.min(...p.legs.map((l) => l.dte));
    body = `<div class="scroll-x legs-wrap"><table class="mini legs"><thead><tr><th>方向</th><th>行权价</th><th>到期日 · 距到期</th><th>买 / 卖</th><th>中间价</th><th>IV</th><th>未平仓</th></tr></thead><tbody>
      ${p.legs.map((l) => `<tr><td>${l.side === "buy" ? "买入" : "卖出"} ${l.right === "call" ? "Call" : "Put"}</td><td>${fx(l.strike)}</td>
        <td title="${esc(l.expiry)}">${esc(l.expiry.slice(5))} · ${l.dte} 天</td><td title="价差为中间价的 ${l.spread_pct}%">${fx(l.bid)} / ${fx(l.ask)}</td>
        <td>${fx(l.mid, Math.abs(l.mid * 100 - Math.round(l.mid * 100)) < 1e-6 ? 2 : 3)}</td>
        <td>${isNum(l.iv) ? l.iv.toFixed(1) + "%" : "—"}</td><td>${usd0(l.open_interest)}</td></tr>`).join("")}</tbody></table></div>`;
    const lp = c.limit_price;
    const limit = !isNum(lp) ? "—" : lp >= 0 ? `${fx(lp)} <small>借记（买入组合）</small>` : `${fx(-lp)} <small>贷记（卖出组合）</small>`;
    metrics = `<div class="pm">
      ${kv("建议限价（每股）", limit, `从中间价开始挂单 · 距到期 ${dte} 天`)}
      ${kv("盈亏平衡点", c.breakevens && c.breakevens.length ? c.breakevens.map((x) => fx(x)).join("、") : "—", "到期时标的价格")}
      ${kv("每张最大收益", c.unlimited_profit ? `<span class="up">无上限</span>` : `<span class="up num">${signedUsd(c.max_profit)}</span>`, "美元")}
      ${kv("每张最大亏损", c.unlimited_loss ? `<span class="down">无上限</span>` : `<span class="down num">${signedUsd(c.max_loss)}</span>`, "美元")}
      ${kv("所需资金或保证金", isNum(c.capital) ? `<span class="num">${usd0(c.capital)}</span> <small>美元 / 张</small>` : "—", esc(c.capital_note || ""))}
    </div>`;
    calcLabel = "张";
  } else {
    const e = p.stock_entry || {};
    const longSide = c.side !== "short";
    body = `<table class="mini legs"><tbody>
      <tr><td>${longSide ? "买入" : "卖空"}价格区间</td><td>${fx(e.low)} – ${fx(e.high)}</td></tr>
      <tr><td>止损价</td><td>${fx(e.stop)}</td></tr><tr><td>目标价</td><td>${fx(e.target)}</td></tr>
      <tr><td>计划持有</td><td>${e.holding_days ? `${e.holding_days} 天` : "—"}</td></tr></tbody></table>`;
    metrics = `<div class="pm">
      ${kv("建议限价", `${fx(e.low)} – ${fx(e.high)}`, "限价单，在区间内挂单")}
      ${kv("每股最大亏损", `<span class="down num">−${fx(c.max_loss_per_share)}</span>`, `按 ${fx(c.worst_entry)} 成交、${fx(e.stop)} 止损`)}
      ${kv("每股目标收益", `<span class="up num">+${fx(c.reward_per_share)}</span>`, c.reward_risk ? `收益风险比 ${fx(c.reward_risk)}` : "")}
    </div>`;
    calcLabel = "股";
  }
  return `<div class="panel plan" data-plan="${i}">
    <div class="plan-head"><span class="tag ${p.type === "option" ? "accent" : ""}">${p.type === "option" ? "期权" : "正股"}</span><h4>${esc(p.structure)}</h4></div>
    ${body}${metrics}
    <div class="plan-text">${rows}<div class="pr"><span class="k">主要风险</span><span>${risks}</span></div></div>
    <div class="calc"><label>单笔可承受亏损 <input class="input small num-in" type="number" min="0" step="50" inputmode="decimal" data-calc="${i}" aria-label="单笔可承受亏损（美元）"> 美元</label>
      <span class="calc-out" data-out="${i}" data-unit="${calcLabel}">—</span></div>
  </div>`;
}

function calc(inp) {
  const i = +inp.dataset.calc, p = A.result && A.result.analysis.trade_plans[i];
  const out = $(`[data-out="${i}"]`);
  if (!p || !out) return;
  const c = p.computed || {};
  const loss = Number(inp.value);
  const unit = out.dataset.unit;
  const per = p.type === "option" ? (c.unlimited_loss ? null : Math.abs(c.max_loss)) : c.max_loss_per_share;
  if (p.type === "option" && c.unlimited_loss) { out.innerHTML = `最大亏损无上限，不能按亏损计算张数`; return; }
  if (!isNum(per) || per <= 0) { out.textContent = "—"; return; }
  if (!inp.value || !isNum(loss) || loss <= 0) { out.innerHTML = `每${unit === "张" ? "张" : "股"}最大亏损 ${fx(per)} 美元`; return; }
  const n = Math.floor(loss / per + 1e-9);
  out.innerHTML = `可${unit === "张" ? "开" : "买"} <b class="num">${n.toLocaleString("en-US")}</b> ${unit} <span class="muted">= ${usd0(loss)} ÷ ${fx(per)}，向下取整</span>`;
}

function watchHTML(a, r) {
  const w = a.watchlist.map((x) => `<tr><td>${esc(x.metric)}</td><td class="wrap">${txt(x.threshold)}</td></tr>`).join("");
  const src = a.sources.map((s) => `<li>${s.url ? `<a class="lnk" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a>` : esc(s.name)}
      ${s.accessed_at ? `<span class="muted">访问于 ${esc(whenET(s.accessed_at))}</span>` : ""}</li>`).join("");
  const u = r.usage || {};
  const foot = r.mode === "mock" ? "模拟模式，未调用模型" : `模型 ${esc(r.model)} · 网络搜索 ${r.searches || 0} 次${r.attempts > 1 ? " · JSON 解析失败后重试 1 次" : ""}${isNum(u.output_tokens) ? ` · 输入 ${usd0(u.input_tokens)} / 输出 ${usd0(u.output_tokens)} tokens` : ""}`;
  return `<div class="sec-label an-label">监控清单</div>
    <div class="panel">${w ? `<table class="grid"><thead><tr><th>指标</th><th>阈值</th></tr></thead><tbody>${w}</tbody></table>` : `<div class="empty">未取得</div>`}</div>
    <div class="sec-label an-label">来源列表</div>
    <div class="panel"><ol class="src-list">${src || `<li class="faint">未取得</li>`}</ol></div>
    <p class="note an-note">${foot}</p>`;
}
