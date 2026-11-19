// 行情监控台 — frontend. No build step; talks only to the local backend (never to OpenD).
import { initAnalysis, onAnalysisProgress, setAnalysisSettings } from "./analysis.js";
import { mountHeatmap, unmountHeatmap } from "./heatmap.js";
// ─────────────────────────────────────────────────────────────── helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const IC = {
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  x: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
};
const icon = (n, size = 16) => `<svg class="ic" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${IC[n]}</svg>`;

const isNum = (v) => typeof v === "number" && isFinite(v);
const fx = (v, d = 2) => (isNum(v) ? v.toFixed(d) : "—");
const px3 = (v) => (!isNum(v) ? "—" : Math.abs(v * 100 - Math.round(v * 100)) < 1e-6 ? v.toFixed(2) : v.toFixed(3));
const signed = (v, d = 2) => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) : "—");
const pctf = (v) => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(2) + "%" : "—");
const vol = (v) => (!isNum(v) ? "—" : v >= 1e8 ? (v / 1e8).toFixed(2) + "亿" : v >= 1e4 ? (v / 1e4).toFixed(1) + "万" : String(Math.round(v)));
const intf = (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : "—");
const ivf = (v) => (isNum(v) ? v.toFixed(1) + "%" : "—");
const usd = (v) => (isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) : "—");
const dir = (v) => (!isNum(v) || v === 0 ? "flat" : v > 0 ? "up" : "down");
const short = (code) => String(code || "").replace(/^US\./, "");

const FMT = {
  last: fx, eff_price: fx, change: signed, change_pct: pctf, high: fx, low: fx, volume: vol, k5_close: fx,
  bid: fx, ask: fx, mid: px3, spread: fx, oi: intf, iv: ivf, strike: fx,
  delta: (v) => fx(v, 3), gamma: (v) => fx(v, 4), theta: (v) => fx(v, 3), vega: (v) => fx(v, 3),
};
const COLORED = new Set(["change", "change_pct"]);   // price-direction colors only on these (+ P&L)
const SESS = { PRE: "盘前", RTH: "盘中", AFTER: "盘后", OVERNIGHT: "夜盘", CLOSED: "休市" };
const SESS_KEY = { PRE: "pre", AFTER: "after", OVERNIGHT: "overnight" };
const MARKET = {
  NONE: "无", AUCTION: "竞价", WAITING_OPEN: "等待开盘", MORNING: "盘中", REST: "午间休市", AFTERNOON: "盘中",
  CLOSED: "已收盘", PRE_MARKET_BEGIN: "盘前交易", PRE_MARKET_END: "盘前结束", AFTER_HOURS_BEGIN: "盘后交易",
  AFTER_HOURS_END: "盘后结束", OVERNIGHT: "夜盘", NIGHT_OPEN: "夜盘", NIGHT_END: "夜盘结束", FUTURE_DAY_OPEN: "日盘",
};
const SUB_SHORT = { QUOTE: "报价", TICKER: "逐笔", ORDER_BOOK: "摆盘", K_1M: "1 分 K", K_5M: "5 分 K" };
const EP = [["get_market_snapshot", "快照"], ["get_option_chain", "期权链"], ["get_option_expiration_date", "到期日"],
  ["place_order", "下单"], ["modify_order", "改撤单"], ["order_list_query", "委托查询"],
  ["get_option_strategy_analysis", "组合报价"], ["request_highest_quote_right", "行情权限"]];
const OS = { UNSUBMITTED: "未提交", WAITING_SUBMIT: "等待提交", SUBMITTING: "提交中", SUBMITTED: "已提交", FILLED_PART: "部分成交",
  FILLED_ALL: "全部成交", CANCELLING_PART: "撤单中", CANCELLING_ALL: "撤单中", CANCELLED_PART: "部分撤单", CANCELLED_ALL: "已撤单",
  FAILED: "失败", SUBMIT_FAILED: "提交失败", DISABLED: "已失效", DELETED: "已删除", TIMEOUT: "超时", FILL_CANCELLED: "成交撤销" };
const PS = { entry_pending: "入场挂单", open: "持仓中", exit_pending: "平仓挂单", closed: "已平仓", cancelled: "已取消", unmanaged: "手动处理" };
const EV = { place: "下单", place_failed: "下单失败", order_update: "委托状态", fill: "成交回报", cancel: "撤单", cancel_failed: "撤单失败",
  cancel_all: "全部撤单", cancel_all_failed: "全部撤单失败", emergency_stop: "紧急停止", auto_switch: "自动下单开关",
  env_switch: "切换环境", limits_changed: "修改硬性限制", skip: "未下单", reject: "拒绝下单", position_open: "开仓成交",
  position_closed: "平仓完成", position_reduced: "部分平仓", auto_off: "自动关闭", combo_check: "组合单检测",
  exit_unmanaged: "转为手动", order_list_failed: "查询委托失败" };
const ENV = { SIMULATE: "模拟交易", REAL: "实盘交易" };
const SIDE = { BUY: "买入", SELL: "卖出", SELL_SHORT: "卖空", BUY_BACK: "买回" };
const LIMITS = [
  ["max_contracts_per_order", "单笔最大合约数", "张"], ["max_orders_per_day", "每日最大自动开仓次数", "次"],
  ["max_daily_loss_usd", "每日最大亏损", "美元"], ["stop_loss_pct", "止损", "% 入场权利金"],
  ["take_profit_pct", "止盈目标", "% 入场权利金"], ["time_stop_et", "时间止损（美东）", ""],
  ["mid_offset", "限价相对中间价的偏移", "美元"], ["entry_timeout_sec", "入场单超时撤单", "秒"],
  ["exit_reprice_sec", "平仓单重新报价间隔", "秒"],
];

function optName(code) {
  const m = /^US\.([A-Z.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d+)$/.exec(code || "");
  if (!m) return short(code);
  return `${m[1]} ${m[3]}/${m[4]} ${+(m[6]) / 1000} ${m[5]}`;
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.detail) || `请求失败（HTTP ${r.status}）`);
  return data;
}
const run = (p) => p.catch((e) => toast("error", e.message));
const msgs = (r) => (r && r.messages || []).forEach((m) => toast("info", m));

// render only when content changed; never steal focus from an input inside
function setHTML(el, html) {
  if (!el || el._html === html) return;
  const a = document.activeElement;
  if (a && el.contains(a) && (a.tagName === "INPUT" || a.tagName === "SELECT")) return;
  el._html = html;
  el.innerHTML = html;
}

// ─────────────────────────────────────────────────────────────── state
const S = {
  mode: "opend", quotes: {}, watchlist: [], subs: {}, subDeadline: {}, status: null,
  options: { underlyings: [], pinned: [] }, chains: {}, exps: {}, atm: {}, alerts: { rules: [], invalid: [], history: [] },
  trading: null, condTypes: {}, stockSubtypes: [], expanded: new Set(), details: {}, holdSec: 60,
  market: null, news: { events: [], earnings: [], settings: null }, sectorDetail: {}, mktExpanded: null,
  newsFilter: { symbol: "", levels: "高,中", source: "" }, newsSources: new Set(), badge: 0,
};

function setSubs(data) {
  S.subs = data || {};
  S.subDeadline = {};
  const now = Date.now();
  for (const [key, types] of Object.entries(S.subs)) {
    for (const [st, v] of Object.entries(types)) if (v.unsub_in != null) S.subDeadline[`${key}|${st}`] = now + v.unsub_in * 1000;
  }
}

// ─────────────────────────────────────────────────────────────── quotes → cells
function sessionHTML(q) {
  const s = q.session || "CLOSED";
  const k = SESS_KEY[s];
  let h = `<span class="tag ${s === "RTH" ? "accent" : ""}">${SESS[s] || s}</span>`;
  if (k && isNum(q[k + "_price"])) {
    h += `<span class="px num">${fx(q[k + "_price"])}</span><span class="num ${dir(q[k + "_change_pct"])}">${pctf(q[k + "_change_pct"])}</span>`;
  }
  const parts = ["pre", "after", "overnight"].filter((p) => isNum(q[p + "_price"]))
    .map((p) => `${{ pre: "盘前", after: "盘后", overnight: "夜盘" }[p]} ${fx(q[p + "_price"])}`);
  return `<span class="sess" title="${esc(parts.join(" · ") || "无盘前盘后数据")}">${h}</span>`;
}

function applyQuote(code, q) {
  for (const el of document.querySelectorAll(`[data-q="${code}"]`)) {
    const f = el.dataset.f;
    if (f === "session") { el.innerHTML = sessionHTML(q); continue; }
    if (f === "name") { el.textContent = q.name || ""; continue; }
    const v = q[f];
    const t = (FMT[f] || fx)(v);
    if (el.textContent !== t) el.textContent = t;
    if (COLORED.has(f)) { el.classList.remove("up", "down", "flat"); el.classList.add(dir(v)); }
  }
}
const cell = (code, f, cls = "r") => `<td class="${cls}" data-q="${esc(code)}" data-f="${f}">—</td>`;
function fillQuotes(root) {
  const seen = new Set($$("[data-q]", root).map((el) => el.dataset.q));
  for (const c of seen) if (S.quotes[c]) applyQuote(c, S.quotes[c]);
}

// ─────────────────────────────────────────────────────────────── status
const fmtET = new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const fmtLocal = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const localTZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
function tickClock() {
  const now = new Date();
  const a = $("#clkET"), b = $("#clkLocal");
  if (a) a.textContent = fmtET.format(now);
  if (b) b.textContent = fmtLocal.format(now);
  for (const el of $$("[data-cd]")) {
    const left = Math.max(0, Math.ceil((+el.dataset.cd - Date.now()) / 1000));
    el.textContent = `${left}s`;
  }
}

function meter(used, total) {
  const pct = isNum(used) && isNum(total) && total > 0 ? Math.min(100, (used / total) * 100) : 0;
  return `<div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div>`;
}

function renderStatus() {
  const s = S.status;
  if (!s) return;
  const connZh = { connected: "已连接", connecting: "连接中", disconnected: "已断开" }[s.connection] || s.connection;
  const live = s.connection === "connected" ? "live" : "";
  const q = s.quota || {};
  const rates = EP.filter(([k]) => s.rate && s.rate[k] && s.rate[k].max_calls != null).map(([k, name]) => {
    const r = s.rate[k];
    const win = r.window_sec !== 30 ? `/${r.window_sec}s` : "";
    const shared = r.shared_by || [];
    const suffix = shared.includes("place_combo_order") ? "·含组合" : shared.includes("cancel_all_order") ? "·含全撤" : "";
    return `<span class="${r.queued ? "busy" : ""}">${name}${suffix}</span><b>${r.used}/${r.max_calls}${win}${r.queued ? ` 排队${r.queued}` : ""}</b>`;
  }).join("");
  const tr = S.trading || {};
  const now = new Date();
  const mk = (s.rate && s.rate.get_market_snapshot && s.rate.get_market_snapshot.consumers || {}).market;
  const rates2 = mk ? rates + `<span>市场页快照</span><b>${mk.used}/${mk.cap}</b>` : rates;
  const html = `
    <div class="stat"><div class="stat-k">OPEND</div><div class="stat-v"><i class="dot ${live}"></i>${connZh}</div>
      <div class="stat-s">${s.mode === "mock" ? "模拟数据" : `行情${s.qot_logined ? "已登录" : "未登录"} · 交易${s.trd_logined ? "已登录" : "未登录"}`}</div></div>
    <div class="stat"><div class="stat-k">行情权限</div><div class="stat-v">美股 ${esc(s.us_qot_right || "—")} · 期权 ${esc(s.us_option_qot_right || "—")}</div>
      <div class="stat-s">${s.right_lost ? "最高权限已被其他终端占用" : `美股市场：${esc(MARKET[s.market_us] || s.market_us || "—")}`}</div></div>
    <div class="stat"><div class="stat-k">股票订阅额度</div><div class="stat-v num">${q.stock_used ?? "—"} / ${q.stock_total ?? "—"}</div>${meter(q.stock_used, q.stock_total)}
      <div class="stat-s">剩余 ${q.stock_remain ?? "—"} 个</div></div>
    <div class="stat"><div class="stat-k" title="期权额度与股票额度相互独立">期权订阅额度</div><div class="stat-v num">${q.option_used ?? "—"} / ${q.option_total ?? "—"}</div>${meter(q.option_used, q.option_total)}
      <div class="stat-s">剩余 ${q.option_remain ?? "—"} 个 · 独立额度</div></div>
    <div class="stat rates-cell"><div class="stat-k">接口频率（当前 30 秒窗口）</div><div class="rates" style="grid-template-columns:auto auto auto auto">${rates2}</div></div>
    <div class="stat"><div class="stat-k">时间</div><div class="stat-v num">美东 <span id="clkET">${fmtET.format(now)}</span></div>
      <div class="stat-s" title="${esc(localTZ)}">本地 <span id="clkLocal">${fmtLocal.format(now)}</span></div></div>
    <div class="stat"><div class="stat-k">交易</div><div class="stat-v">${ENV[s.env] || s.env}<span class="tag ${s.auto_enabled ? "accent" : ""}">自动下单${s.auto_enabled ? "开" : "关"}</span></div>
      <div class="stat-s">${s.trade_ready ? "交易连接就绪" : "交易连接未就绪"}${tr.sim_account ? ` · 模拟 ${esc(tr.sim_account)}` : ""}</div></div>`;
  setHTML($("#statusGrid"), html);
  const ns = s.news_sources || [];
  const fmtT = (t) => (t ? fmtET.format(new Date(t * 1000)) : "—");
  setHTML($("#newsRow"), `<div class="src-line"><span class="stat-k">资讯来源</span>${ns.map((x) => {
    const ok = x.enabled && x.last_ok && !x.last_error;
    const state = !x.enabled ? "已关闭" : x.last_error ? `<span class="err">错误</span>` : x.note ? esc(x.note) : x.last_ok ? `最近成功 ${fmtT(x.last_ok)}` : "等待首次轮询";
    const tip = [x.last_error ? `最近错误（美东 ${fmtT(x.error_at)}）：${x.last_error}` : "", x.note || ""].filter(Boolean).join("；");
    return `<span class="s" title="${esc(tip)}"><i class="dot ${ok ? "ok" : ""}"></i>${esc(x.label)} · ${state}</span>`;
  }).join("")}</div>`);
  $("#rightLost").hidden = !s.right_lost;
  $("#mockRow").hidden = s.mode !== "mock";
  $("#modeTag").textContent = s.mode === "mock" ? "模拟模式" : s.connection === "connected" ? "OpenD 实时" : "OpenD 未连接";
}

function setNavConn(ok) {
  const el = $("#navConn");
  el.innerHTML = `<i class="dot ${ok ? "live" : ""}"></i><span>${ok ? "看板已连接" : "看板连接断开，正在重连"}</span>`;
}

// ─────────────────────────────────────────────────────────────── stocks
function chipsHTML(key, types, extraCls = "") {
  return `<div class="chips ${extraCls}">${types.map((st) => {
    const sub = (S.subs[key] || {})[st];
    const dl = S.subDeadline[`${key}|${st}`];
    let cls = "", extra = "";
    if (sub && dl) { cls = "pending"; extra = ` <span class="cd" data-cd="${dl}"></span>`; }
    else if (sub) cls = "on";
    const title = sub && dl ? "退订已预约：订阅需保持至少 1 分钟，到期自动退订（点击可取消退订）" : sub ? "已订阅，点击取消（占用 1 个股票订阅额度）" : "点击订阅（占用 1 个股票订阅额度）";
    return `<button class="chip ${cls}" data-act="sub" data-code="${esc(key)}" data-st="${st}" title="${title}">${SUB_SHORT[st] || st}${extra}</button>`;
  }).join("")}</div>`;
}

function renderStocks() {
  const types = S.stockSubtypes.map((x) => x.key);
  const rows = S.watchlist.map((w) => {
    const c = w.code;
    const q = S.quotes[c] || {};
    const open = S.expanded.has(c);
    let h = `<tr class="clickable" data-code="${esc(c)}" data-act="expand">
      <td><div class="code">${esc(short(c))}</div><div class="code-sub" data-q="${esc(c)}" data-f="name">${esc(q.name || "")}</div></td>
      <td data-q="${esc(c)}" data-f="session"></td>
      ${cell(c, "last")}${cell(c, "change")}${cell(c, "change_pct")}${cell(c, "high")}${cell(c, "low")}${cell(c, "volume")}${cell(c, "k5_close")}
      <td>${chipsHTML(c, types)}</td>
      <td class="r"><button class="icon-btn" data-act="rm-stock" data-code="${esc(c)}" title="移除">${icon("x")}</button></td></tr>`;
    if (open) h += `<tr class="detail" data-detail="${esc(c)}"><td colspan="11">${detailHTML(c)}</td></tr>`;
    return h;
  }).join("");
  const tb = $("#stockTable tbody");
  setHTML(tb, rows || `<tr><td colspan="11" class="empty">还没有自选。输入代码添加，例如 US.QQQ、US.NVDA。</td></tr>`);
  fillQuotes(tb);
  $("#stockNote").textContent = `每种订阅类型每个代码占用 1 个股票订阅额度；订阅需保持至少 ${Math.round(S.holdSec / 60)} 分钟才能取消，提前取消会在到期后自动退订。5 分钟收盘为最近一根已完成的 K 线。`;
}

function detailHTML(code) {
  const d = S.details[code] || {};
  const subs = S.subs[code] || {};
  const book = d.book;
  let bookH = `<div class="hint">勾选“摆盘”后显示买卖盘</div>`;
  if (subs.ORDER_BOOK && book) {
    const asks = (book.asks || []).slice(0, 5).reverse(), bids = (book.bids || []).slice(0, 5);
    bookH = `<table class="mini">${asks.map((a, i) => `<tr><td class="muted">卖 ${asks.length - i}</td><td>${fx(a[0])}</td><td>${intf(a[1])}</td></tr>`).join("")}
      ${bids.map((b, i) => `<tr><td class="muted">买 ${i + 1}</td><td>${fx(b[0])}</td><td>${intf(b[1])}</td></tr>`).join("")}</table>`;
  }
  let tickH = `<div class="hint">勾选“逐笔”后显示成交明细</div>`;
  if (subs.TICKER && d.ticks) {
    tickH = `<table class="mini">${d.ticks.slice(-8).reverse().map((t) => `<tr><td class="muted">${esc(String(t.time || "").slice(11))}</td><td>${fx(t.price)}</td><td>${intf(t.volume)}</td></tr>`).join("")}</table>`;
  }
  const kl = (k, label) => {
    const arr = d[k] || [];
    if (!subs[k]) return `<div class="hint">勾选“${SUB_SHORT[k]}”后显示 ${label}</div>`;
    return `<table class="mini">${arr.slice(-4).reverse().map((c) => `<tr><td class="muted">${esc(String(c.time_key || "").slice(11, 16))}</td><td>开 ${fx(c.open)}</td><td>收 ${fx(c.close)}</td></tr>`).join("")}</table>`;
  };
  return `<div class="detail-grid">
    <div><h4>摆盘</h4>${bookH}</div>
    <div><h4>逐笔</h4>${tickH}</div>
    <div><h4>K 线（时间为 K 线结束时间，美东）</h4><div class="sec-label" style="margin:2px 0 4px">1 分钟</div>${kl("K_1M", "1 分钟 K 线")}
      <div class="sec-label" style="margin:10px 0 4px">5 分钟</div>${kl("K_5M", "5 分钟 K 线")}</div></div>`;
}

async function loadDetail(code) {
  try {
    const d = await api("GET", `/api/stocks/${encodeURIComponent(code)}/detail`);
    S.details[code] = { ...(S.details[code] || {}), ...d };
    renderDetail(code);
  } catch { /* ignore */ }
}
function renderDetail(code) {
  const row = document.querySelector(`tr[data-detail="${code}"] > td`);
  if (row) row.innerHTML = detailHTML(code);
}

// ─────────────────────────────────────────────────────────────── options
const ckey = (u, d) => `${u}|${d}`;
async function loadExps(u, refresh = false) {
  try {
    const r = await api("GET", `/api/options/expirations/${encodeURIComponent(u)}${refresh ? "?refresh=true" : ""}`);
    S.exps[u] = r.expirations;
    renderUnderlyings();
  } catch (e) { toast("error", e.message); }
}

function renderOptions() {
  renderPins();
  renderUnderlyings();
  renderChains();
  $("#optNote").textContent = "每条期权链每天获取一次并缓存。快照轮询不占用订阅额度（默认 5 秒，最快 1 秒，每次最多 400 个合约）；订阅模式每条期权链每种数据类型占用 1 个期权订阅额度（报价 + 摆盘 = 2 个）。";
}

function renderPins() {
  const pins = S.options.pinned || [];
  const tb = $("#pinTable tbody");
  const rows = pins.map((c) => {
    const m = /([CP])(\d+)$/.exec(c);
    const q = S.quotes[c] || {};
    return `<tr data-code="${esc(c)}"><td><div class="code">${esc(optName(c))}</div><div class="code-sub">${esc(c)}</div></td>
      <td>${m && m[1] === "C" ? "看涨" : "看跌"}</td>${cell(c, "strike")}<td class="num">${esc(q.expiry || "")}</td>
      ${cell(c, "bid")}${cell(c, "ask")}${cell(c, "mid")}${cell(c, "spread")}${cell(c, "last")}${cell(c, "volume")}${cell(c, "oi")}
      ${cell(c, "iv")}${cell(c, "delta")}${cell(c, "gamma", "r hide-md")}${cell(c, "theta", "r hide-md")}${cell(c, "vega", "r hide-md")}
      <td class="r"><button class="icon-btn" data-act="unpin" data-code="${esc(c)}" title="取消固定">${icon("x")}</button></td></tr>`;
  }).join("");
  setHTML(tb, rows || `<tr><td colspan="17" class="empty">还没有固定的合约。在下方期权链中点击合约旁的 + 固定到期权自选。</td></tr>`);
  fillQuotes(tb);
}

function renderUnderlyings() {
  const box = $("#underlyings");
  const html = (S.options.underlyings || []).map((u) => {
    const sel = new Set((u.expirations || []).map((e) => e.date));
    const exps = S.exps[u.code];
    const chips = exps ? exps.map((e) => `<button class="exp ${sel.has(e.date) ? "on" : ""}" data-act="exp" data-u="${esc(u.code)}" data-d="${e.date}">${e.date.slice(5)}<span class="dte">${e.dte ?? ""}天</span></button>`).join("")
      : `<span class="hint">正在加载到期日……</span>`;
    return `<div class="panel" data-under="${esc(u.code)}"><div class="under-head">
        <div class="under-title"><span class="code">${esc(short(u.code))}</span>
          <span class="num" data-q="${esc(u.code)}" data-f="eff_price">—</span><span class="num" data-q="${esc(u.code)}" data-f="change_pct">—</span>
          <span class="hint">选择到期日后显示期权链（可多选）</span></div>
        <div class="ctrls"><button class="btn small" data-act="refresh-exps" data-u="${esc(u.code)}">${icon("refresh", 14)}刷新到期日</button>
          <button class="icon-btn" data-act="rm-under" data-u="${esc(u.code)}" title="移除标的">${icon("x")}</button></div></div>
      <div class="exp-row">${chips}</div></div>`;
  }).join("");
  setHTML(box, html || `<div class="panel"><div class="empty">还没有期权标的。输入标的代码（例如 US.QQQ）后选择到期日。</div></div>`);
  fillQuotes(box);
}

function renderChains() {
  const box = $("#chains");
  const want = [];
  for (const u of S.options.underlyings || []) for (const e of u.expirations || []) want.push(ckey(u.code, e.date));
  for (const el of $$("[data-chain]", box)) if (!want.includes(el.dataset.chain)) el.remove();
  for (const k of want) {
    let el = box.querySelector(`[data-chain="${k}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "panel";
      el.dataset.chain = k;
      el.innerHTML = `<div class="chain-head"></div>
        <p class="chain-legend">绿色底色为看涨，红色底色为看跌，颜色较深为实值，较浅为虚值，黄色为该侧最大未平仓。</p>
        <div class="scroll-x"><table class="grid chain"><thead></thead><tbody></tbody></table></div>`;
      box.append(el);
    }
    renderChain(k);
  }
  // keep DOM order = config order
  for (const k of want) box.append(box.querySelector(`[data-chain="${k}"]`));
}

function expCfg(u, d) {
  const ui = (S.options.underlyings || []).find((x) => x.code === u);
  return ui && (ui.expirations || []).find((e) => e.date === d);
}

// underlying price for a chain: picks the ATM row and decides in / out of the money
function underPrice(u) {
  const q = S.quotes[u] || {};
  return isNum(q.eff_price) ? q.eff_price : q.last;
}

function atmIndex(m) {
  const p = underPrice(m.underlying);
  if (!m.strikes || !m.strikes.length) return -1;
  if (!isNum(p)) return Math.floor(m.strikes.length / 2);
  let best = 0;
  m.strikes.forEach((r, i) => { if (Math.abs(r.strike - p) < Math.abs(m.strikes[best].strike - p)) best = i; });
  return best;
}

const CALL_COLS = [["vega", "Vega", "hide-md"], ["theta", "Theta", "hide-md"], ["gamma", "Gamma", "hide-md"], ["delta", "Delta", ""], ["iv", "隐含波动率", ""],
  ["oi", "未平仓", ""], ["volume", "成交量", ""], ["last", "最新", ""], ["mid", "中间价", ""], ["bid", "买价", ""], ["ask", "卖价", ""]];
const PUT_COLS = [...CALL_COLS].reverse().map(([f, n, c]) => [f, n, c]);
// puts: bid/ask next to the strike, greeks outside
PUT_COLS.splice(0, 2, ["bid", "买价", ""], ["ask", "卖价", ""]);

function renderChain(k) {
  const el = $(`#chains [data-chain="${k}"]`);
  const [u, d] = k.split("|");
  const m = S.chains[k];
  const cfg = expCfg(u, d) || {};
  if (!el) return;
  const mode = cfg.mode || "snapshot";
  const subs = S.subs[`CHAIN|${u}|${d}`] || {};
  const pending = Object.keys(subs).filter((st) => S.subDeadline[`CHAIN|${u}|${d}|${st}`]);
  const subInfo = mode === "subscribe"
    ? `<span class="hint">订阅：${Object.keys(subs).map((s) => SUB_SHORT[s] || s).join(" + ") || "—"}（占用 ${Object.keys(subs).length} 个期权额度）</span>`
    : pending.length ? `<span class="hint">订阅退订中：<span data-cd="${S.subDeadline[`CHAIN|${u}|${d}|${pending[0]}`]}"></span></span>` : "";
  const head = `<div class="chain-title"><h3>${esc(short(u))} · ${esc(d)}</h3><span class="tag">${m && m.dte != null ? `${m.dte} 天` : ""}</span>
      <span class="hint">${m && m.fetched_at ? `期权链缓存于 ${esc(m.fetched_at.slice(5))}（美东）` : "期权链加载中……"}</span></div>
    <div class="ctrls">
      <div class="seg small" role="group" aria-label="报价模式">
        <button class="${mode === "snapshot" ? "active" : ""}" data-act="mode" data-key="${esc(k)}" data-mode="snapshot" title="不占用订阅额度">快照轮询</button>
        <button class="${mode === "subscribe" ? "active" : ""}" data-act="mode" data-key="${esc(k)}" data-mode="subscribe" title="每种数据类型每条期权链占用 1 个期权订阅额度">订阅</button></div>
      ${mode === "snapshot" ? `<label>间隔 <input class="input small num-in" type="number" min="1" step="1" value="${cfg.interval_sec ?? 5}" data-chg="interval" data-key="${esc(k)}"> 秒</label>` : ""}
      <label>行权价上下各 <input class="input small num-in" type="number" min="1" max="100" step="1" value="${cfg.strikes_each_side ?? 8}" data-chg="strikes" data-key="${esc(k)}"> 档</label>
      ${subInfo}
      <button class="btn small" data-act="refresh-chain" data-key="${esc(k)}" title="重新获取期权链（期权链接口每 30 秒最多 10 次）">${icon("refresh", 14)}刷新期权链</button>
      <button class="icon-btn" data-act="exp" data-u="${esc(u)}" data-d="${esc(d)}" title="关闭此到期日">${icon("x")}</button></div>`;
  setHTML($(".chain-head", el), head);
  if (!m || !m.strikes || !m.strikes.length) { setHTML($("tbody", el), `<tr><td class="empty">期权链加载中……</td></tr>`); return; }
  const idx = atmIndex(m);
  S.atm[k] = idx;
  const n = cfg.strikes_each_side ?? 8;
  const rows = m.strikes.slice(Math.max(0, idx - n), idx + n + 1);
  const thead = `<tr><th class="side call" colspan="${CALL_COLS.length + 1}">看涨 CALL</th><th class="strike"></th><th class="side put" colspan="${PUT_COLS.length + 1}">看跌 PUT</th></tr>
    <tr><th></th>${CALL_COLS.map(([, nm, c]) => `<th class="r ${c}">${nm}</th>`).join("")}<th class="strike">行权价</th>
    ${PUT_COLS.map(([, nm, c]) => `<th class="r ${c}">${nm}</th>`).join("")}<th></th></tr>`;
  setHTML($("thead", el), thead);
  const pins = new Set(S.options.pinned || []);
  const body = rows.map((r) => {
    const atm = m.strikes[idx] && r.strike === m.strikes[idx].strike;
    const pinBtn = (c) => c ? (pins.has(c) ? `<span class="faint" title="已在期权自选">✓</span>`
      : `<button class="icon-btn pin" data-act="pin" data-code="${esc(c)}" title="固定到期权自选">${icon("plus", 14)}</button>`) : "";
    // every cell of a side carries "call" / "put"; tintChain adds itm / otm
    const side = (c, cols, sd) => cols.map(([f, , cls]) => c ? `<td class="r ${cls} ${sd}" data-q="${esc(c)}" data-f="${f}">—</td>` : `<td class="${cls} ${sd}"></td>`).join("");
    return `<tr class="${atm ? "atm" : ""}" data-k="${r.strike}"><td class="c">${pinBtn(r.call)}</td>${side(r.call, CALL_COLS, "call")}
      <td class="strike num">${fx(r.strike)}${atm ? `<span class="atm-tag">平值</span>` : ""}</td>${side(r.put, PUT_COLS, "put")}<td class="c">${pinBtn(r.put)}</td></tr>`;
  }).join("");
  const tb = $("tbody", el);
  tb._html = null;
  tb.innerHTML = body;
  tb._codes = new Set(rows.flatMap((r) => [r.call, r.put]).filter(Boolean));
  tintChain(tb, underPrice(u));
  fillQuotes(tb);
  markMaxOI(tb);
}

// call ITM when strike < underlying, put ITM when strike > underlying; ITM cells get the stronger tint
function tintChain(tb, p) {
  for (const tr of $$("tr[data-k]", tb)) {
    const k = Number(tr.dataset.k);
    const itm = { call: isNum(p) && k < p, put: isNum(p) && k > p };
    for (const td of tr.children) {
      const sd = td.classList.contains("call") ? "call" : td.classList.contains("put") ? "put" : null;
      if (!sd) continue;
      td.classList.toggle("itm", itm[sd]);
      td.classList.toggle("otm", !itm[sd]);
    }
  }
}

// Largest 未平仓 per side among the rows shown, compared on the raw quote numbers (missing = 0).
// Ties are all marked; nothing is marked while every value is 0.
function markMaxOI(tb) {
  const oi = (el) => { const v = Number((S.quotes[el.dataset.q] || {}).oi); return Number.isFinite(v) ? v : 0; };
  for (const sd of ["call", "put"]) {
    const cells = $$(`td.${sd}[data-f="oi"]`, tb);
    const max = cells.reduce((mx, el) => Math.max(mx, oi(el)), 0);
    for (const el of cells) {
      const on = max > 0 && oi(el) === max;
      if (el.classList.contains("oi-max") === on) continue;
      el.classList.toggle("oi-max", on);
      if (on) el.title = "该侧最大未平仓"; else el.removeAttribute("title");
    }
  }
}

// after each quote batch: re-mark the chains showing any of those contracts
// (snapshot polling and subscription pushes both arrive as "quotes")
function remarkChains(batch) {
  const codes = Object.keys(batch);
  for (const tb of $$("#chains [data-chain] tbody")) {
    if (tb._codes && codes.some((c) => tb._codes.has(c))) markMaxOI(tb);
  }
}

function onUnderlyingMove(code) {
  for (const [k, m] of Object.entries(S.chains)) {
    if (m.underlying !== code) continue;
    if (atmIndex(m) !== S.atm[k]) { renderChain(k); continue; }
    // same ATM row, but the price may have crossed a displayed strike
    const tb = $(`#chains [data-chain="${k}"] tbody`);
    if (tb) tintChain(tb, underPrice(code));
  }
}

// ─────────────────────────────────────────────────────────────── alerts
function renderAlerts() {
  const A = S.alerts;
  const auto = S.trading && S.trading.auto_enabled;
  const rows = (A.rules || []).map((r) => {
    const w = r.active_window_et ? `${r.active_window_et[0]}–${r.active_window_et[1]}` : "全天";
    const trig = r.trigger_once ? "仅一次" : `冷却 ${r.cooldown_sec} 秒后再次触发`;
    const st = r.status;
    const stCls = st === "matched" || st === "fired" ? "accent" : st === "watching" ? "" : "";
    const oa = r.order_action;
    const order = oa && oa.enabled ? `<span class="tag ${auto ? "accent" : ""}">${oa.type === "combo" ? "组合单" : `${SIDE[oa.side] || oa.side} ${oa.quantity} 张`}</span>${auto ? "" : `<div class="hint">总开关关闭，不会下单</div>`}` : `<span class="faint">—</span>`;
    return `<tr data-rule="${esc(r.id)}" data-code="${esc(r.symbol)}">
      <td><div class="code">${esc(r.name)}</div><div class="code-sub">已触发 ${r.fired_count || 0} 次</div></td>
      <td class="num">${esc(short(r.symbol))}</td>
      <td style="white-space:normal; min-width:240px">${(r.descriptions || []).map(esc).join("<br>且 ")}${(r.hints || []).map((h) => `<div class="hint">${esc(h)}</div>`).join("")}</td>
      <td class="num">${w}</td><td>${trig}</td>
      <td><span class="tag ${stCls}">${esc(r.status_zh)}</span></td><td>${order}</td>
      <td class="r" style="white-space:nowrap">
        <button class="tg" role="switch" aria-checked="${r.enabled}" data-act="rule-toggle" data-id="${esc(r.id)}" title="${r.enabled ? "停用" : "启用"}"></button>
        ${st === "fired" ? `<button class="btn ghost small" data-act="rule-rearm" data-id="${esc(r.id)}">重新启用</button>` : ""}
        <button class="btn ghost small" data-act="rule-edit" data-id="${esc(r.id)}">编辑</button>
        <button class="icon-btn" data-act="rule-del" data-id="${esc(r.id)}" title="删除">${icon("trash")}</button></td></tr>`;
  });
  for (const bad of A.invalid || []) {
    rows.push(`<tr><td>${esc(bad.raw && bad.raw.name || "（无名称）")}</td><td>${esc(bad.raw && bad.raw.symbol || "")}</td>
      <td colspan="6" class="muted">配置文件中的规则无效：${esc(bad.error)}</td></tr>`);
  }
  setHTML($("#ruleTable tbody"), rows.join("") || `<tr><td colspan="8" class="empty">还没有提醒规则。点击“新建规则”添加。</td></tr>`);
  const hist = (A.history || []).slice(0, 30).map((h) => `<div class="hist-item"><span class="t">${esc(h.time_et)}</span><span>${esc(h.message)}${h.order_linked ? ` <span class="tag">已关联下单</span>` : ""}</span></div>`).join("");
  setHTML($("#alertHistory"), hist || `<div class="empty">暂无触发记录。</div>`);
}

// alert fired → highlight rows, desktop notification, sound
let actx = null;
function beep() {
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const t = actx.currentTime;
    [0, 0.2].forEach((dt) => {
      const o = actx.createOscillator(), g = actx.createGain();
      o.type = "sine"; o.frequency.value = dt ? 1175 : 880;
      g.gain.setValueAtTime(0.0001, t + dt);
      g.gain.exponentialRampToValueAtTime(0.3, t + dt + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.16);
      o.connect(g).connect(actx.destination);
      o.start(t + dt); o.stop(t + dt + 0.18);
    });
  } catch { /* audio unavailable */ }
}
document.addEventListener("pointerdown", () => {
  try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); } catch { /* */ }
}, { passive: true });

function flash(el) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
function onAlert(a) {
  toast("alert", `提醒触发（美东 ${a.time_et}）：${a.message}`);
  beep();
  for (const sym of a.symbols || [a.symbol]) {
    $$(`tr[data-code="${sym}"]`).forEach(flash);
    $$(`[data-q="${sym}"]`).forEach(flash);
  }
  $$(`tr[data-rule="${a.rule_id}"]`).forEach(flash);
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification("行情提醒触发", { body: a.message, tag: a.rule_id + ":" + a.ts, requireInteraction: false }); } catch { /* */ }
  }
}
function renderNotifyRow() {
  const row = $("#notifyRow");
  if (!("Notification" in window)) { row.hidden = false; row.querySelector(".msg span").textContent = "此浏览器不支持桌面通知，提醒触发时仅高亮并播放提示音。"; $("#btnNotify").hidden = true; return; }
  if (Notification.permission === "granted") { row.hidden = true; return; }
  row.hidden = false;
  if (Notification.permission === "denied") {
    row.querySelector(".msg span").textContent = "桌面通知已被浏览器禁止。请在浏览器的网站设置中允许 127.0.0.1 发送通知。";
    $("#btnNotify").hidden = true;
  }
}

// ─────────────────────────────────────────────────────────────── trading
function renderTrading() {
  const t = S.trading;
  if (!t) return;
  const realOpts = (t.real_accounts || []).map((a) => `<option value="${esc(a.acc_id)}" ${a.acc_id === t.real_account_id ? "selected" : ""}>${esc(a.label)}</option>`).join("");
  const combo = t.combo || {};
  const comboZh = { unknown: "未检测", supported: "支持", unsupported: "不支持" }[combo.status] || combo.status;
  setHTML($("#tradeSwitches"), `
    <div class="prow"><div class="lbl">自动下单总开关<small>关闭时任何规则都不会下单；程序每次启动时都为关闭</small></div>
      <div class="val"><button class="tg" role="switch" aria-checked="${t.auto_enabled}" data-act="auto" ${t.trade_ready ? "" : "disabled"} aria-label="自动下单总开关"></button></div></div>
    <div class="prow"><div class="lbl">交易环境<small>默认模拟交易。切换到实盘需要两步确认，并在 OpenD 界面手动解锁交易</small></div>
      <div class="val"><div class="seg black small">
        <button class="${t.env === "SIMULATE" ? "active" : ""}" data-act="env" data-env="SIMULATE">模拟交易</button>
        <button class="${t.env === "REAL" ? "active" : ""}" data-act="env" data-env="REAL">实盘交易</button></div></div></div>
    <div class="prow"><div class="lbl">模拟账户<small>自动识别支持美股期权的模拟账户</small></div><div class="val"><strong class="num">${esc(t.sim_account || "未找到")}</strong></div></div>
    <div class="prow"><div class="lbl">实盘账户<small>${t.env === "REAL" ? "实盘交易中不能更换账户" : "切换到实盘交易前需先选择"}</small></div>
      <div class="val"><select class="input small" data-chg="realacc" ${t.env === "REAL" ? "disabled" : ""}><option value="">请选择</option>${realOpts}</select></div></div>
    <div class="prow"><div class="lbl">组合单<small>${esc(combo.message || "")}${combo.paper_verified ? " · 已在模拟盘受理" : " · 实盘需先在模拟盘成功下过组合单"}</small></div>
      <div class="val"><span class="tag ${combo.status === "supported" ? "accent" : ""}">${esc(comboZh)}</span></div></div>
    <div class="prow"><div class="lbl">交易连接</div><div class="val">${t.trade_ready ? "已就绪" : "未就绪"}</div></div>`);
  const dv = t.daily || {};
  setHTML($("#tradeToday"), `
    <div class="prow"><div class="lbl">自动开仓次数<small>平仓与止损不受此限制</small></div><div class="val"><strong class="num">${dv.entries ?? 0} / ${dv.max_orders ?? "—"}</strong></div></div>
    <div class="prow"><div class="lbl">已实现盈亏</div><div class="val num ${dir(dv.realized)}">${usd(dv.realized)} 美元</div></div>
    <div class="prow"><div class="lbl">未实现盈亏<small>按中间价估算</small></div><div class="val num ${dir(dv.unrealized)}">${usd(dv.unrealized)} 美元</div></div>
    <div class="prow"><div class="lbl">当日合计<small>亏损达到 ${dv.max_loss ?? "—"} 美元时自动关闭总开关</small></div>
      <div class="val"><strong class="num ${dir(dv.total)}">${usd(dv.total)} 美元</strong>${dv.loss_locked ? `<span class="tag dark">今日已锁定</span>` : ""}</div></div>`);
  const L = t.limits || {};
  setHTML($("#tradeLimits"), LIMITS.map(([k, label, unit]) =>
    `<div class="prow"><div class="lbl">${label}</div><div class="val"><strong class="num">${esc(L[k] ?? "—")}</strong> ${unit}</div></div>`).join(""));
  const pos = (t.positions || []).map((p) => {
    const pnl = p.status === "closed" ? p.pnl : p.unrealized;
    const contract = p.kind === "combo" ? (p.legs || []).map((l) => esc(`${SIDE[l.side]} ${l.ratio}× ${optName(l.code)}`)).join("<br>") : esc(optName(p.code));
    return `<tr><td class="num">${esc(String(p.created_et || "").slice(11))}</td><td>${esc(p.rule_name)}</td><td>${contract}</td><td>${SIDE[p.side] || p.side}</td>
      <td class="r num">${p.filled_qty || p.qty}</td><td class="r num">${fx(p.entry_price ?? p.entry_limit)}</td>
      ${p.kind === "single" ? cell(p.code, "mid") : `<td class="r">—</td>`}
      <td class="r num ${dir(pnl)}">${usd(pnl)}</td><td>${ENV[p.env] || p.env}</td><td><span class="tag">${PS[p.status] || p.status}</span></td>
      <td class="muted" style="white-space:normal">${esc(p.exit_reason || "")} ${esc(p.note || "")}</td></tr>`;
  }).join("");
  const ptb = $("#posTable tbody");
  setHTML(ptb, pos || `<tr><td colspan="11" class="empty">暂无托管持仓。</td></tr>`);
  fillQuotes(ptb);
  const ords = (t.orders || []).map((o) => `<tr><td class="num">${esc(String(o.create_time || "").slice(11))}</td><td class="num muted">${esc(o.order_id)}</td>
    <td>${esc(optName(o.code))}</td><td>${SIDE[o.trd_side] || esc(o.trd_side)}</td><td class="r num">${o.qty ?? "—"}</td><td class="r num">${fx(o.price)}</td>
    <td class="r num">${o.dealt_qty ?? 0}</td><td class="r num">${fx(o.dealt_avg_price)}</td><td><span class="tag">${OS[o.order_status] || esc(o.order_status)}</span></td>
    <td>${ENV[o.trd_env] || esc(o.trd_env || "")}</td><td class="muted">${esc(o.remark || o.last_err_msg || "")}</td></tr>`).join("");
  setHTML($("#orderTable tbody"), ords || `<tr><td colspan="11" class="empty">今日暂无委托。</td></tr>`);
  const logs = (t.log || []).slice().reverse().map((e) => {
    const parts = [];
    if (e.contract) parts.push(typeof e.contract === "string" ? optName(e.contract) : JSON.stringify(e.contract));
    if (e.side) parts.push(SIDE[e.side] || e.side);
    if (e.qty != null) parts.push(`${e.qty} 张`);
    if (e.price != null) parts.push(`@ ${fx(+e.price)}`);
    if (e.status) parts.push(OS[e.status] || e.status);
    if (e.rule) parts.push(`规则：${e.rule}`);
    if (e.reason) parts.push(e.reason);
    if (e.env) parts.push(ENV[e.env] || e.env);
    if (e.event === "auto_switch") parts.push(e.enabled ? "开启" : "关闭");
    if (e.event.endsWith("failed") && e.raw) parts.push(String(e.raw).slice(0, 120));
    return `<div class="log-item"><span class="t">${esc(String(e.ts_et || "").slice(11))}</span><span class="e">${EV[e.event] || esc(e.event)}</span><span class="d" title="${esc(parts.join(" · "))}">${esc(parts.join(" · "))}</span></div>`;
  }).join("");
  setHTML($("#logList"), logs || `<div class="empty">今日暂无订单日志。</div>`);
}

// ─────────────────────────────────────────────────────────────── sheet + toast
function openSheet(html, trigger) {
  const sh = $("#sheet");
  sh.innerHTML = html;
  sh.classList.remove("closing");
  if (trigger && trigger.getBoundingClientRect) {
    const r = trigger.getBoundingClientRect();
    const w = sh.offsetWidth, h = sh.offsetHeight;
    const ox = Math.max(0, Math.min(w, r.left + r.width / 2 - (innerWidth - w) / 2));
    const oy = Math.max(0, Math.min(h, r.top + r.height / 2 - (innerHeight - h) / 2));
    sh.style.transformOrigin = `${ox}px ${oy}px`;
  }
  requestAnimationFrame(() => { sh.classList.add("open"); $("#scrim").classList.add("open"); });
  $$("[data-close]", sh).forEach((b) => (b.onclick = closeSheet));
  setTimeout(() => { const f = sh.querySelector("input:not([disabled]),select:not([disabled])"); if (f) f.focus(); }, 60);
  return sh;
}
function closeSheet() {
  const sh = $("#sheet");
  sh.classList.add("closing");
  sh.classList.remove("open");
  $("#scrim").classList.remove("open");
}
$("#scrim").addEventListener("click", closeSheet);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

function toast(level, text, ms) {
  const box = $("#toasts");
  while (box.children.length >= 5) box.firstChild.remove();
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = `<span class="bar"></span><div>${esc(text)}</div>`;
  el.onclick = () => el.remove();
  box.append(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const t = ms ?? (level === "error" ? 9000 : level === "alert" ? 12000 : level === "warn" ? 9000 : 4500);
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 400); }, t);
}

// ─────────────────────────────────────────────────────────────── rule editor
function condRow(c, i, total) {
  const opts = Object.entries(S.condTypes).map(([k, label]) => `<option value="${k}" ${c.type === k ? "selected" : ""}>${esc(label)}</option>`).join("");
  const vals = c.type === "price_in_range"
    ? `<input class="input small v" data-c="low" type="number" step="any" placeholder="下限" value="${c.low ?? ""}"><span class="muted">~</span><input class="input small v" data-c="high" type="number" step="any" placeholder="上限" value="${c.high ?? ""}">`
    : `<input class="input small v" data-c="value" type="number" step="any" placeholder="${c.type === "news_high_within" ? "分钟" : "数值"}" value="${c.value ?? ""}">`;
  return `<div class="cond" data-i="${i}"><select class="input small" data-c="type">${opts}</select>${vals}
    <input class="input small sym" data-c="symbol" placeholder="代码（默认同标的）" value="${esc(c.symbol || "")}">
    ${total > 1 ? `<button class="icon-btn" type="button" data-c="rm" title="删除条件">${icon("x", 14)}</button>` : ""}</div>`;
}

function contractOptions(selected) {
  const set = new Set(S.options.pinned || []);
  if (selected) set.add(selected);
  return `<option value="">请选择合约（期权自选）</option>` + [...set].map((c) => `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(optName(c))}</option>`).join("");
}

function openRuleEditor(rule, trigger) {
  const r = rule ? JSON.parse(JSON.stringify(rule)) : {
    name: "", symbol: "", enabled: true, active_window_et: null, conditions: [{ type: "price_above", value: "" }],
    trigger_once: true, cooldown_sec: 300, order_action: null,
  };
  const auto = !!(S.trading && S.trading.auto_enabled);
  const wasLinked = !!(r.order_action && r.order_action.enabled);
  const oa = r.order_action || { enabled: false, type: "single", side: "BUY", contract: null, quantity: 1, legs: [] };
  if (!oa.legs || !oa.legs.length) oa.legs = [{ code: "", side: "BUY", ratio: 1 }, { code: "", side: "SELL", ratio: 1 }];
  const comboOK = !(S.trading && S.trading.combo && S.trading.combo.status === "unsupported");
  const maxC = (S.trading && S.trading.limits && S.trading.limits.max_contracts_per_order) || 1;

  const draw = () => {
    const newsOnly = r.conditions.length > 0 && r.conditions.every((c) => c.type === "news_high_within");
    if (newsOnly) oa.enabled = false;
    const linked = !!oa.enabled;
    const lockOrder = !auto;
    const legs = oa.legs.map((l, i) => `<div class="cond" data-leg="${i}">
        <select class="input small" data-l="code">${contractOptions(l.code)}</select>
        <div class="seg small"><button type="button" class="${l.side === "BUY" ? "active" : ""}" data-l="side" data-v="BUY" ${lockOrder ? "disabled" : ""}>买入</button><button type="button" class="${l.side === "SELL" ? "active" : ""}" data-l="side" data-v="SELL" ${lockOrder ? "disabled" : ""}>卖出</button></div>
        <input class="input small v" data-l="ratio" type="number" min="1" max="10" step="1" value="${l.ratio || 1}" title="比例">
        ${oa.legs.length > 2 ? `<button class="icon-btn" type="button" data-l="rm">${icon("x", 14)}</button>` : ""}</div>`).join("");
    return `<h3 id="sheetTitle">${rule ? "编辑提醒规则" : "新建提醒规则"}</h3>
    <div class="form" id="ruleForm">
      <label class="field"><span>名称</span><input class="input" data-r="name" value="${esc(r.name)}" placeholder="例如 QQQ 回补缺口"></label>
      <label class="field"><span>标的代码（股票、ETF 或期权合约）</span><input class="input" data-r="symbol" value="${esc(r.symbol)}" placeholder="例如 US.QQQ 或 US.QQQ261113C675000"></label>
      <div class="field"><span>生效时段（美东时间，可留空表示全天）</span><div class="row">
        <input class="input small" type="time" data-r="w0" value="${r.active_window_et ? r.active_window_et[0] : ""}"><span class="muted">至</span>
        <input class="input small" type="time" data-r="w1" value="${r.active_window_et ? r.active_window_et[1] : ""}"></div></div>
      <div class="field"><span>条件（全部满足才触发；“≥ / ≤”包含等于）</span>
        <div class="group" id="conds">${r.conditions.map((c, i) => condRow(c, i, r.conditions.length)).join("")}
          <div><button class="btn small" type="button" id="addCond">${icon("plus", 14)}添加条件</button></div></div></div>
      <div class="field"><span>触发方式</span><div class="row">
        <div class="seg small"><button type="button" class="${r.trigger_once ? "active" : ""}" data-trig="once">仅触发一次</button><button type="button" class="${r.trigger_once ? "" : "active"}" data-trig="repeat">冷却后再次触发</button></div>
        ${r.trigger_once ? "" : `<label class="row" style="gap:6px">冷却 <input class="input small num-in" type="number" min="1" data-r="cooldown" value="${r.cooldown_sec || 300}"> 秒</label>`}</div></div>
      <div class="field"><span>关联自动下单</span>
        <div class="group">
          <div class="row" style="justify-content:space-between"><span class="warn-text">${newsOnly ? "只包含新闻条件的规则不能关联下单：新闻只说明发生了重要事件，不能单独作为下单依据。请至少再添加一个价格条件（与新闻条件为“且”）" : auto ? "触发时按提交时的中间价下限价单（受硬性限制约束）" : "自动下单总开关关闭：不能新增或修改下单动作"}</span>
            <button class="tg" type="button" role="switch" aria-checked="${linked}" id="oaToggle" ${(newsOnly || (!auto && !wasLinked)) ? "disabled" : ""}></button></div>
          ${linked ? `
          <div class="row"><div class="seg small"><button type="button" class="${oa.type === "single" ? "active" : ""}" data-oa="single" ${lockOrder ? "disabled" : ""}>单个合约</button>
            <button type="button" class="${oa.type === "combo" ? "active" : ""}" data-oa="combo" ${lockOrder || !comboOK ? "disabled" : ""} title="${comboOK ? "" : "账户不支持组合单"}">组合单</button></div>
            <label class="row" style="gap:6px">数量 <input class="input small num-in" type="number" min="1" max="${maxC}" step="1" data-r="qty" value="${oa.quantity || 1}" ${lockOrder ? "disabled" : ""}> 张（上限 ${maxC}）</label></div>
          ${oa.type === "combo" ? `<div class="hint">组合单作为一笔订单提交，价格取 get_option_strategy_analysis 的中间价；仅支持净借方组合。先点击检测。</div>${legs}
            <div class="row"><button class="btn small" type="button" id="addLeg" ${lockOrder || oa.legs.length >= 4 ? "disabled" : ""}>${icon("plus", 14)}添加一条腿</button>
            <button class="btn small" type="button" id="checkCombo">检测组合单支持（模拟盘）</button></div>`
          : `<div class="row"><div class="seg small"><button type="button" class="${oa.side === "BUY" ? "active" : ""}" data-side="BUY" ${lockOrder ? "disabled" : ""}>买入</button><button type="button" class="${oa.side === "SELL" ? "active" : ""}" data-side="SELL" ${lockOrder ? "disabled" : ""}>卖出</button></div>
            <select class="input small" data-r="contract" style="flex:1 1 220px" ${lockOrder ? "disabled" : ""}>${contractOptions(oa.contract)}</select></div>`}
          <div class="hint">限价单，限价 = 提交时中间价 ± 偏移（偏移在硬性限制中设置）。</div>` : ""}
        </div></div>
      <div class="err" id="ruleErr"></div>
    </div>
    <div class="acts"><button class="primary" id="saveRule">保存</button><button class="ghost" data-close>取消</button></div>`;
  };

  const sync = () => {   // read form → r / oa
    const f = $("#ruleForm");
    if (!f) return;
    r.name = $('[data-r="name"]', f).value.trim();
    r.symbol = $('[data-r="symbol"]', f).value.trim();
    const w0 = $('[data-r="w0"]', f).value, w1 = $('[data-r="w1"]', f).value;
    r.active_window_et = w0 && w1 ? [w0, w1] : null;
    r.conditions = $$(".cond[data-i]", f).map((row) => {
      const c = { type: $('[data-c="type"]', row).value };
      const sym = $('[data-c="symbol"]', row).value.trim();
      if (sym) c.symbol = sym;
      const num = (k) => { const el = $(`[data-c="${k}"]`, row); return el && el.value !== "" ? Number(el.value) : el ? el.value : undefined; };
      if (c.type === "price_in_range") { c.low = num("low"); c.high = num("high"); } else c.value = num("value");
      return c;
    });
    const cd = $('[data-r="cooldown"]', f);
    if (cd) r.cooldown_sec = Number(cd.value);
    const q = $('[data-r="qty"]', f);
    if (q) oa.quantity = Number(q.value);
    const ct = $('[data-r="contract"]', f);
    if (ct) oa.contract = ct.value || null;
    $$(".cond[data-leg]", f).forEach((row, i) => {
      oa.legs[i].code = $('[data-l="code"]', row).value;
      oa.legs[i].ratio = Number($('[data-l="ratio"]', row).value || 1);
    });
  };

  const sh = openSheet(draw(), trigger);
  const redraw = () => { sh.innerHTML = draw(); bind(); };   // callers sync() before changing r / oa
  const bind = () => {
    $$("[data-close]", sh).forEach((b) => (b.onclick = closeSheet));
    $("#addCond", sh).onclick = () => { sync(); r.conditions.push({ type: "price_above", value: "" }); redraw(); };
    $$('.cond[data-i] [data-c="type"]', sh).forEach((s) => (s.onchange = () => { sync(); redraw(); }));
    $$('.cond[data-i] [data-c="rm"]', sh).forEach((b) => (b.onclick = () => { sync(); r.conditions.splice(+b.closest(".cond").dataset.i, 1); redraw(); }));
    $$("[data-trig]", sh).forEach((b) => (b.onclick = () => { sync(); r.trigger_once = b.dataset.trig === "once"; redraw(); }));
    const tg = $("#oaToggle", sh);
    if (tg) tg.onclick = () => { sync(); oa.enabled = !oa.enabled; redraw(); };
    $$("[data-oa]", sh).forEach((b) => (b.onclick = () => { sync(); oa.type = b.dataset.oa; redraw(); }));
    $$("[data-side]", sh).forEach((b) => (b.onclick = () => { sync(); oa.side = b.dataset.side; redraw(); }));
    $$('[data-l="side"]', sh).forEach((b) => (b.onclick = () => { sync(); oa.legs[+b.closest(".cond").dataset.leg].side = b.dataset.v; redraw(); }));
    $$('[data-l="rm"]', sh).forEach((b) => (b.onclick = () => { sync(); oa.legs.splice(+b.closest(".cond").dataset.leg, 1); redraw(); }));
    const al = $("#addLeg", sh);
    if (al) al.onclick = () => { sync(); oa.legs.push({ code: "", side: "BUY", ratio: 1 }); redraw(); };
    const cc = $("#checkCombo", sh);
    if (cc) cc.onclick = async () => {
      sync();
      cc.disabled = true;
      try {
        const res = await api("POST", "/api/trading/combo_check", { legs: oa.legs });
        $("#ruleErr", sh).textContent = res.status === "supported" ? "检测通过：模拟盘接受该组合单" : `检测未通过：${res.message}`;
      } catch (e) { $("#ruleErr", sh).textContent = e.message; }
      cc.disabled = false;
    };
    $("#saveRule", sh).onclick = async () => {
      sync();
      const body = { ...r };
      delete body.status; delete body.status_zh; delete body.descriptions; delete body.hints; delete body.fired_count; delete body.last_fired_ts;
      if (oa.enabled) {
        body.order_action = { enabled: true, type: oa.type, quantity: oa.quantity, limit_price: "mid" };
        if (oa.type === "combo") body.order_action.legs = oa.legs; else { body.order_action.side = oa.side; body.order_action.contract = oa.contract; }
      } else body.order_action = r.order_action ? { ...r.order_action, enabled: false } : null;
      try {
        await api(rule ? "PUT" : "POST", rule ? `/api/alerts/${encodeURIComponent(rule.id)}` : "/api/alerts", body);
        closeSheet();
        toast("info", rule ? "规则已保存" : "规则已创建");
      } catch (e) { $("#ruleErr", sh).textContent = e.message; }
    };
  };
  bind();
}

// ─────────────────────────────────────────────────────────────── limits / env dialogs
function openLimitsEditor(trigger) {
  const L = (S.trading && S.trading.limits) || {};
  const fields = LIMITS.map(([k, label, unit]) => `<label class="field"><span>${label}${unit ? `（${unit}）` : ""}</span>
    <input class="input" data-lim="${k}" ${k === "time_stop_et" ? 'type="time"' : 'type="number" step="any"'} value="${esc(L[k] ?? "")}"></label>`).join("");
  const sh = openSheet(`<h3 id="sheetTitle">修改硬性限制</h3><p class="lead">这些限制写在配置文件中，自动下单每一笔都会检查。保存前需要再次确认。</p>
    <div class="form">${fields}<div class="err" id="limErr"></div></div>
    <div class="acts"><button class="primary" id="limNext">下一步</button><button class="ghost" data-close>取消</button></div>`, trigger);
  $("#limNext", sh).onclick = () => {
    const nv = {};
    $$("[data-lim]", sh).forEach((i) => { nv[i.dataset.lim] = i.dataset.lim === "time_stop_et" ? i.value : Number(i.value); });
    const changes = LIMITS.filter(([k]) => String(nv[k]) !== String(L[k])).map(([k, label, unit]) => `<div class="prow" style="padding-left:0;padding-right:0;min-height:44px"><div class="lbl" style="font-size:14px">${label}</div><div class="val num">${esc(L[k])} → <strong>${esc(nv[k])}</strong> ${unit}</div></div>`).join("");
    if (!changes) { $("#limErr", sh).textContent = "没有修改"; return; }
    sh.innerHTML = `<h3 id="sheetTitle">确认修改硬性限制？</h3><p class="lead">修改会立即生效并写入配置文件。</p><div>${changes}</div><div class="err" id="limErr"></div>
      <div class="acts"><button class="dark" id="limOk">确认修改</button><button class="ghost" data-close>取消</button></div>`;
    $$("[data-close]", sh).forEach((b) => (b.onclick = closeSheet));
    $("#limOk", sh).onclick = async () => {
      try { await api("PUT", "/api/trading/limits", { limits: nv, confirmed: true }); closeSheet(); toast("info", "硬性限制已更新"); }
      catch (e) { $("#limErr", sh).textContent = e.message; }
    };
  };
}

async function switchEnv(env, trigger) {
  if (!S.trading || env === S.trading.env) return;
  const r = await api("POST", "/api/trading/env", { env });
  if (r.done) { toast("info", env === "SIMULATE" ? "已切换到模拟交易，自动下单已关闭" : "已是实盘交易"); return; }
  const sh = openSheet(`<h3 id="sheetTitle">切换到实盘交易？</h3>
    <p class="lead">实盘交易将使用账户 <b>${esc(r.account)}</b> 的真实资金。切换后自动下单会先关闭，需要你重新开启。<br>
    你还需要在 OpenD 界面中手动解锁交易；本程序不会输入、保存或记录任何密码。<br>建议先在模拟交易中完成全部验收。</p>
    <div class="acts"><button class="dark" id="envStep1">继续（第 1 步，共 2 步）</button><button class="ghost" data-close>取消</button></div>`, trigger);
  $("#envStep1", sh).onclick = () => {
    sh.innerHTML = `<h3 id="sheetTitle">第 2 步：输入确认文字</h3><p class="lead">请输入“${esc(r.phrase)}”以确认切换到实盘交易。确认在 2 分钟内有效。</p>
      <div class="form"><input class="input" id="envPhrase" placeholder="${esc(r.phrase)}" autocomplete="off"><div class="err" id="envErr"></div></div>
      <div class="acts"><button class="dark" id="envStep2" disabled>确认切换到实盘交易</button><button class="ghost" data-close>取消</button></div>`;
    $$("[data-close]", sh).forEach((b) => (b.onclick = closeSheet));
    const inp = $("#envPhrase", sh), btn = $("#envStep2", sh);
    inp.focus();
    inp.oninput = () => { btn.disabled = inp.value.trim() !== r.phrase; };
    btn.onclick = async () => {
      try { await api("POST", "/api/trading/env/confirm", { challenge: r.challenge, phrase: inp.value }); closeSheet(); }
      catch (e) { $("#envErr", sh).textContent = e.message; }
    };
  };
}

function openMockPrice(trigger) {
  const sh = openSheet(`<h3 id="sheetTitle">模拟：设置标的价格</h3><p class="lead">用于在模拟模式下测试提醒与止损。</p>
    <div class="form"><label class="field"><span>代码</span><input class="input" id="mpCode" value="${esc((S.watchlist[0] || {}).code || "US.QQQ")}"></label>
    <label class="field"><span>价格</span><input class="input" id="mpPrice" type="number" step="any"></label><div class="err" id="mpErr"></div></div>
    <div class="acts"><button class="primary" id="mpOk">设置</button><button class="ghost" data-close>取消</button></div>`, trigger);
  $("#mpOk", sh).onclick = async () => {
    try {
      const r = await api("POST", "/api/mock/set_price", { code: $("#mpCode", sh).value.trim().toUpperCase(), price: Number($("#mpPrice", sh).value) });
      closeSheet(); toast("info", r.message);
    } catch (e) { $("#mpErr", sh).textContent = e.message; }
  };
}

// ─────────────────────────────────────────────────────────────── events
async function emergencyStop(btn) {
  btn.disabled = true;
  try { await api("POST", "/api/trading/emergency_stop"); }
  catch (e) { toast("error", `紧急停止失败：${e.message}`); }
  finally { setTimeout(() => (btn.disabled = false), 800); }
}
$("#btnStop").addEventListener("click", (e) => emergencyStop(e.currentTarget));
$("#btnStop2").addEventListener("click", (e) => emergencyStop(e.currentTarget));
$("#btnRight").addEventListener("click", (e) => {
  const b = e.currentTarget; b.disabled = true;
  run(api("POST", "/api/status/quote_right").then((r) => toast(r.ok ? "info" : "error", r.ok ? `已发送请求。OpenD 返回：${r.text || "成功"}` : r.text))).finally(() => (b.disabled = false));
});
$("#btnNotify").addEventListener("click", async () => { await Notification.requestPermission(); renderNotifyRow(); });
$("#btnMockPrice").addEventListener("click", (e) => openMockPrice(e.currentTarget));
$("#btnNewRule").addEventListener("click", (e) => openRuleEditor(null, e.currentTarget));
$("#btnLimits").addEventListener("click", (e) => openLimitsEditor(e.currentTarget));

$("#addStock").addEventListener("submit", (e) => {
  e.preventDefault();
  const inp = e.currentTarget.code;
  const code = inp.value.trim();
  if (!code) return;
  run(api("POST", "/api/stocks", { code }).then((r) => { inp.value = ""; toast("info", r.message); }));
});
$("#addUnderlying").addEventListener("submit", (e) => {
  e.preventDefault();
  const inp = e.currentTarget.code;
  const code = inp.value.trim();
  if (!code) return;
  run(api("POST", "/api/options/underlyings", { code }).then((r) => { inp.value = ""; S.exps[r.code] = r.expirations; renderUnderlyings(); }));
});

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-act]");
  if (!t || t.disabled) return;
  const a = t.dataset.act;
  if (a === "expand") {
    if (e.target.closest("button, input, select, a")) return;
    const c = t.dataset.code;
    if (S.expanded.has(c)) S.expanded.delete(c); else { S.expanded.add(c); loadDetail(c); }
    $("#stockTable tbody")._html = null;
    renderStocks();
    return;
  }
  e.stopPropagation();
  if (a === "sub") {
    const code = t.dataset.code, st = t.dataset.st;
    const sub = (S.subs[code] || {})[st];
    const pending = S.subDeadline[`${code}|${st}`];
    const enable = !sub || !!pending;
    const q = S.status && S.status.quota;
    if (enable && !sub && q && isNum(q.stock_remain) && q.stock_remain < 1) {
      toast("error", `股票订阅额度不足：剩余 ${q.stock_remain} 个，未发送订阅请求。请先取消其他订阅。`);
      return;
    }
    t.disabled = true;
    run(api("POST", `/api/stocks/${encodeURIComponent(code)}/subtypes`, { subtype: st, enabled: enable }).then((r) => {
      (r.messages || []).filter((m) => !m.startsWith("已订阅")).forEach((m) => toast("info", m));
      if (S.expanded.has(code)) loadDetail(code);
    })).finally(() => (t.disabled = false));
  } else if (a === "rm-stock") {
    run(api("DELETE", `/api/stocks/${encodeURIComponent(t.dataset.code)}`).then((r) => { msgs(r); S.expanded.delete(t.dataset.code); }));
  } else if (a === "exp") {
    const u = t.dataset.u, d = t.dataset.d;
    const selected = !!expCfg(u, d);
    if (selected) run(api("DELETE", `/api/options/expirations/${encodeURIComponent(u)}/${d}`).then(msgs));
    else run(api("POST", "/api/options/expirations", { underlying: u, date: d }).then((m) => { S.chains[ckey(u, d)] = m; renderChains(); }));
  } else if (a === "rm-under") {
    run(api("DELETE", `/api/options/underlyings/${encodeURIComponent(t.dataset.u)}`).then(msgs));
  } else if (a === "refresh-exps") {
    loadExps(t.dataset.u, true);
  } else if (a === "mode") {
    const [u, d] = t.dataset.key.split("|");
    if ((expCfg(u, d) || {}).mode === t.dataset.mode) return;
    const q = S.status && S.status.quota;
    if (t.dataset.mode === "subscribe" && q && isNum(q.option_remain) && q.option_remain < 2) {
      toast("error", `期权订阅额度不足：订阅模式需要 2 个额度（报价 + 摆盘），当前剩余 ${q.option_remain} 个。未发送订阅请求。`);
      return;
    }
    run(api("PATCH", `/api/options/expirations/${encodeURIComponent(u)}/${d}`, { mode: t.dataset.mode }).then((r) => { S.chains[t.dataset.key] = r.meta; msgs(r); }));
  } else if (a === "refresh-chain") {
    const [u, d] = t.dataset.key.split("|");
    t.disabled = true;
    run(api("POST", `/api/options/chains/${encodeURIComponent(u)}/${d}/refresh`).then((m) => { S.chains[t.dataset.key] = m; renderChain(t.dataset.key); toast("info", "期权链已刷新"); })).finally(() => (t.disabled = false));
  } else if (a === "pin") {
    run(api("POST", "/api/options/pinned", { code: t.dataset.code }).then(() => toast("info", `已固定 ${optName(t.dataset.code)}`)));
  } else if (a === "unpin") {
    run(api("DELETE", `/api/options/pinned/${encodeURIComponent(t.dataset.code)}`));
  } else if (a === "rule-edit") {
    openRuleEditor((S.alerts.rules || []).find((r) => r.id === t.dataset.id), t);
  } else if (a === "rule-del") {
    run(api("DELETE", `/api/alerts/${encodeURIComponent(t.dataset.id)}`).then(() => toast("info", "规则已删除")));
  } else if (a === "rule-rearm") {
    run(api("POST", `/api/alerts/${encodeURIComponent(t.dataset.id)}/rearm`).then(() => toast("info", "规则已重新启用")));
  } else if (a === "rule-toggle") {
    run(api("POST", `/api/alerts/${encodeURIComponent(t.dataset.id)}/enabled`, { enabled: t.getAttribute("aria-checked") !== "true" }));
  } else if (a === "auto") {
    run(api("POST", "/api/trading/auto", { enabled: t.getAttribute("aria-checked") !== "true" }));
  } else if (a === "env") {
    run(switchEnv(t.dataset.env, t));
  }
});

document.addEventListener("change", (e) => {
  const t = e.target;
  const kind = t.dataset && t.dataset.chg;
  if (!kind) return;
  if (kind === "interval" || kind === "strikes") {
    const [u, d] = t.dataset.key.split("|");
    const body = kind === "interval" ? { interval_sec: Number(t.value) } : { strikes_each_side: Number(t.value) };
    run(api("PATCH", `/api/options/expirations/${encodeURIComponent(u)}/${d}`, body).then((r) => { S.chains[t.dataset.key] = r.meta; t.blur(); }));
  } else if (kind === "realacc" && t.value) {
    run(api("POST", "/api/trading/real_account", { acc_id: t.value }).then((r) => toast("info", `实盘账户：${r.real_account}`)));
  }
});

$$("[data-mock]").forEach((b) => b.addEventListener("click", () => run(api("POST", `/api/mock/${b.dataset.mock}`, {}).then((r) => toast("info", r.message)))));

// tabs
function showTab(tab) {
  const btn = $(`#tabs button[data-tab="${tab}"]`);
  if (!$(`#view-${tab}`) || !btn || btn.hidden) tab = "stocks";
  for (const b of $$("#tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const v of $$(".view")) v.hidden = v.id !== `view-${tab}`;
  localStorage.setItem("tab", tab);
  if (tab === "market") { localStorage.setItem("mktSeen", String(Date.now() / 1000)); S.badge = 0; updateBadge(); }
  // the heatmap widget lives only while its tab is shown: mounted on show, emptied on leave
  if (tab === "heatmap") { if (!$("#heatmapHost").firstChild) mountHeatmap($("#heatmapHost")); } else unmountHeatmap();
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); });
initAnalysis({ api, toast });
let wantTab = localStorage.getItem("tab") || "stocks";   // a hidden tab (个股分析 while off) falls back to 股票
showTab(wantTab);

// scroll-edge nav
addEventListener("scroll", () => $("#nav").classList.toggle("scrolled", scrollY > 8), { passive: true });

// ─────────────────────────────────────────────────────────────── market overview
const money = (v) => {
  if (!isNum(v)) return "—";
  const a = Math.abs(v), sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return sign + (a >= 1e8 ? (a / 1e8).toFixed(2) + " 亿" : a >= 1e4 ? (a / 1e4).toFixed(0) + " 万" : a.toFixed(0));
};
const dollars = (v) => (!isNum(v) ? "—" : v >= 1e8 ? (v / 1e8).toFixed(2) + " 亿" : v >= 1e4 ? (v / 1e4).toFixed(0) + " 万" : String(Math.round(v)));
const CHANGE_METHOD = {
  plate_snapshot: "板块涨跌幅直接取自 moomoo 板块快照",
  weighted: "板块涨跌幅按成分股市值加权计算",
  mixed: "板块涨跌幅取自 moomoo 板块快照，缺失的按成分股市值加权计算",
};
const fmtLocalHM = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });

function sessionCaption(m, key) {
  const t = m.updated && m.updated[key];
  if (!t) return "尚未更新";
  return m.session === "RTH" ? `更新于 美东 ${t.slice(11, 19)}` : `非交易时段：显示 美东 ${t.slice(0, 16)} 的数据`;
}

function sectorRow(sec, kind, listId) {
  const open = S.mktExpanded === `${listId}|${sec.code}`;
  let right;
  if (kind === "flow") {
    right = `<span class="v ${dir(sec.flow)}">${money(sec.flow)}</span><span class="m ${dir(sec.change_pct)}">${pctf(sec.change_pct)}</span>`;
  } else {
    const ud = isNum(sec.up) ? `涨 ${sec.up} · 跌 ${sec.down}` : "—";
    right = `<span class="v ${dir(sec.change_pct)}">${pctf(sec.change_pct)}</span><span class="m">${ud}</span>`;
  }
  const extra = kind === "flow" && isNum(sec.coverage)
    ? `<small>前 ${sec.covered}${sec.top && sec.covered < sec.top ? `/${sec.top}` : ""} 只 · 占成交额 ${sec.coverage}%</small>`
    : sec.constituents ? `<small>${sec.constituents} 只</small>` : "";
  let h = `<div class="mrow clickable" data-act="sector" data-list="${listId}" data-code="${esc(sec.code)}" title="点击查看成分股">
    <span class="nm">${esc(sec.name)}${extra}</span>${right}</div>`;
  if (open) h += `<div class="msub">${sectorDetailHTML(sec.code)}</div>`;
  return h;
}

function sectorDetailHTML(code) {
  const d = S.sectorDetail[code];
  if (!d) return `<div class="hint" style="padding:8px 0">正在加载成分股……</div>`;
  if (!d.constituents.length) return `<div class="hint" style="padding:8px 0">暂无成分股数据</div>`;
  return `<table class="mini">${d.constituents.slice(0, 40).map((c) => `<tr>
    <td><span class="code">${esc(short(c.code))}</span> <span class="muted">${esc(c.name || "")}</span>${c.passes ? "" : ' <span class="faint" title="未达到规模门槛">·</span>'}</td>
    <td class="${dir(c.change_pct)}">${pctf(c.change_pct)}</td><td>${fx(c.last)}</td>
    <td class="muted">市值 ${dollars(c.market_cap)}</td><td class="muted">成交额 ${dollars(c.dollar_volume)}</td></tr>`).join("")}</table>`;
}

function renderMarket() {
  const m = S.market;
  if (!m) return;
  $$("#plateSet button").forEach((b) => b.classList.toggle("active", b.dataset.ps === m.plate_set));
  let sub = `${m.plate_sets[m.plate_set]} · ${m.sector_count} 个板块`;
  if (m.loading) sub += ` · 正在加载${m.plate_sets[m.loading.set]}成分股 ${m.loading.done}/${m.loading.total}（接口每 30 秒最多 10 次，首次需要几分钟，之后每天一次并缓存）`;
  const b = m.budget && m.budget.get_market_snapshot;
  if (b) sub += ` · 本页快照用量 ${b.used}/${b.cap}（每 30 秒，占快照总额度的一半）`;
  $("#mktSub").textContent = sub;
  setHTML($("#benchStrip"), (m.benchmarks || []).map((x) => `<div class="bench-item" data-code="${esc(x.code)}">
      <span class="code">${esc(short(x.code))}</span><span class="v">${fx(x.last)}</span><span class="p ${dir(x.change_pct)}">${pctf(x.change_pct)}</span></div>`).join("")
    + `<div class="bench-edit"><button class="btn ghost small" data-act="bench-edit">编辑基准</button></div>`);
  const tn = m.top_n || { sectors: 5, flows: 10, leaders: 5 };
  $("#lblGainers").textContent = `涨幅最大的 ${tn.sectors} 个板块`;
  $("#lblLosers").textContent = `跌幅最大的 ${tn.sectors} 个板块`;
  $("#lblInflow").textContent = `净流入最多的 ${tn.flows} 个板块`;
  $("#lblOutflow").textContent = `净流出最多的 ${tn.flows} 个板块`;
  const uo = m.us_only || {};
  const cs = uo.china_sources || {};
  const usNote = uo.enabled
    ? ` · 仅美股：已剔除 ${uo.china_codes || 0} 只中概股（moomoo 中概股板块 + SEC 营业地址在中国大陆 / 香港 / 澳门的公司，台湾公司保留）${cs.sec_error ? `（${cs.sec_error}）` : ""}，已隐藏 ${(uo.hidden_plates || []).length} 个中概主题板块${(uo.hidden_plates || []).length ? `（${uo.hidden_plates.slice(0, 3).join("、")}${uo.hidden_plates.length > 3 ? " 等" : ""}）` : ""}，`
      + `${uo.recomputed ? `${uo.recomputed} 个含中概股的板块已剔除中概股后按美股成分股市值加权重算` : "其余板块均不含中概股"}`
    : "";
  $("#cap1").textContent = `${sessionCaption(m, "sectors")} · ${CHANGE_METHOD[m.change_method] || "等待数据"}${usNote} · 点击板块查看成分股`;
  const flowNote = m.flow_method === "etf_proxy"
    ? "资金流使用板块 ETF 作为代理（如 XLK、XLF、XLE、SMH），并非板块本身"
    : (() => {
      const fr = m.flow_rules || {};
      const n = fr.top_per_sector || 5, rd = fr.round || {};
      const parts = [`只统计各板块成交额前 ${n} 的成分股（规模达标的美股），板块资金流为这 ${n} 只的当日净流入之和（moomoo 不支持板块级资金流）`,
        `每轮最多查询 ${fr.calls_per_cycle || 60} 只${isNum(rd.seconds) ? `，上一轮 ${rd.codes} 只用时 ${Math.round(rd.seconds)} 秒` : "，约 1–2 分钟完成"}；`
        + `${rd.missing ? `还有 ${rd.missing} 只待首次查询，连续补查中` : `之后每 ${Math.round((fr.refresh_sec || 300) / 60)} 分钟一轮，最久未更新的先查`}`];
      if (isNum(m.flow_coverage)) parts.push(`这 ${n} 只平均占板块成交额 ${m.flow_coverage}%`);
      if (fr.below_threshold) parts.push(`${fr.below_threshold} 个板块因前 ${n} 只占板块成交额不到 ${fr.min_coverage_pct}% 未列入`);
      return parts.join("；");
    })();
  const flowWhen = m.updated && m.updated.flows ? sessionCaption(m, "flows") : (m.inflow || []).length || (m.outflow || []).length ? "首轮计算中（每轮约 1–2 分钟）" : "尚未更新";
  $("#cap2").textContent = `${flowWhen} · ${flowNote}${uo.enabled ? " · 已剔除中概股" : ""} · 资金流只统计常规交易时段，不含盘前盘后`;
  const sf = m.size_filter || {};
  $("#cap3").textContent = `${sessionCaption(m, "sectors")} · 涨幅最大的 ${tn.leaders} 个板块领涨股 · 只统计市值和今日成交额都达到门槛的美股（不含中概股），避免流动性差的小盘股占据榜首`;
  const mc = $("#minCap"), md = $("#minDv");
  if (document.activeElement !== mc) mc.value = Math.round((sf.min_market_cap_usd || 0) / 1e8);
  if (document.activeElement !== md) md.value = Math.round((sf.min_dollar_volume_usd || 0) / 1e4);
  const list = (rows, kind, empty, id) => rows && rows.length ? rows.map((r) => sectorRow(r, kind, id)).join("") : `<div class="empty">${empty}</div>`;
  const waiting = m.loading ? "板块数据加载中……" : "暂无数据";
  const noData = !m.updated || !m.updated.sectors;
  setHTML($("#gainers"), list(m.gainers, "chg", noData ? waiting : "当前没有上涨的板块", "g"));
  setHTML($("#losers"), list(m.losers, "chg", noData ? waiting : "当前没有下跌的板块", "l"));
  setHTML($("#inflow"), list(m.inflow, "flow", m.updated && m.updated.flows ? "暂无净流入板块" : "资金流计算中（每 5 分钟一轮）……", "i"));
  setHTML($("#outflow"), list(m.outflow, "flow", m.updated && m.updated.flows ? "暂无净流出板块" : "资金流计算中……", "o"));
  const rows = (m.leaders || []).map((l) => {
    const cat = l.catalyst;
    const catH = cat ? `<span class="catalyst">${cat.url ? `<a class="lnk" href="${esc(cat.url)}" target="_blank" rel="noopener noreferrer" title="${esc(cat.title)}">${esc(cat.title)}</a>` : esc(cat.title)}</span>` : `<span class="faint">—</span>`;
    return `<tr data-code="${esc(l.code)}"><td>${esc(l.sector)}</td><td><span class="code">${esc(short(l.code))}</span> <span class="muted">${esc(l.name || "")}</span></td>
      <td class="r ${dir(l.change_pct)}">${pctf(l.change_pct)}</td><td class="r">${fx(l.last)}</td><td class="r">${dollars(l.dollar_volume)}</td><td>${catH}</td></tr>`;
  }).join("");
  setHTML($("#leaderTable tbody"), rows || `<tr><td colspan="6" class="empty">${m.loading ? "成分股加载中……" : "暂无达到规模门槛的领涨股"}</td></tr>`);
}

async function loadSector(code) {
  try {
    S.sectorDetail[code] = await api("GET", `/api/market/sector/${encodeURIComponent(code)}`);
  } catch (e) {
    S.sectorDetail[code] = { constituents: [] };
    toast("error", e.message);
  }
  ["#gainers", "#losers", "#inflow", "#outflow"].forEach((s) => ($(s)._html = null));
  renderMarket();
}

function openBenchEditor(trigger) {
  const codes = (S.market && S.market.benchmark_codes) || [];
  const sh = openSheet(`<h3 id="sheetTitle">编辑基准 ETF</h3><p class="lead">每行或用逗号分隔一个代码（moomoo 格式，如 US.SPY）。指数请使用对应 ETF。</p>
    <div class="form"><textarea class="input" id="benchCodes">${esc(codes.join("\n"))}</textarea><div class="err" id="benchErr"></div></div>
    <div class="acts"><button class="primary" id="benchOk">保存</button><button class="ghost" data-close>取消</button></div>`, trigger);
  $("#benchOk", sh).onclick = async () => {
    const list = $("#benchCodes", sh).value.split(/[\s,，]+/).filter(Boolean);
    try { S.market = await api("PUT", "/api/market/benchmarks", { codes: list }); renderMarket(); closeSheet(); }
    catch (e) { $("#benchErr", sh).textContent = e.message; }
  };
}

// ─────────────────────────────────────────────────────────────── news
const LV_CLS = { "高": "h", "中": "m", "低": "l" };
let newsTimer = null;
function scheduleNews() {
  clearTimeout(newsTimer);
  newsTimer = setTimeout(() => loadNews().catch(() => {}), 800);
}
async function loadNews() {
  const f = S.newsFilter;
  const r = await api("GET", `/api/news?symbol=${encodeURIComponent(f.symbol)}&levels=${encodeURIComponent(f.levels)}&source=${encodeURIComponent(f.source)}`);
  S.news.events = r.events;
  S.news.earnings = r.earnings;
  renderNews();
}

function renderNewsFilters() {
  const syms = [...new Set([...(S.watchlist || []).map((w) => w.code), ...((S.options.underlyings || []).map((u) => u.code)),
    ...(((S.news.settings || {}).extra_keywords || []).map((k) => "KW:" + k))])];
  const symSel = $("#newsSymbol");
  const cur = S.newsFilter.symbol;
  const symHtml = `<option value="">全部标的</option>` + syms.map((s) => `<option value="${esc(s)}" ${s === cur ? "selected" : ""}>${esc(s.startsWith("KW:") ? "关键词：" + s.slice(3) : short(s))}</option>`).join("");
  if (symSel._html !== symHtml && document.activeElement !== symSel) { symSel.innerHTML = symHtml; symSel._html = symHtml; }
  for (const e of S.news.events || []) for (const s of e.sources || []) S.newsSources.add(s);
  const srcSel = $("#newsSource");
  const curS = S.newsFilter.source;
  const srcHtml = `<option value="">全部来源</option>` + [...S.newsSources].sort().map((s) => `<option value="${esc(s)}" ${s === curS ? "selected" : ""}>${esc(s)}</option>`).join("");
  if (srcSel._html !== srcHtml && document.activeElement !== srcSel) { srcSel.innerHTML = srcHtml; srcSel._html = srcHtml; }
  $$("#newsLevel button").forEach((b) => b.classList.toggle("active", b.dataset.lv === S.newsFilter.levels));
}

function eventHTML(e) {
  const d = new Date(e.published * 1000);
  const dateOnly = e.time_precision === "date";
  const when = dateOnly
    ? `<b>${esc((e.time_et || "").slice(5, 10))}</b><span>来源只提供日期</span>`
    : `<b>${esc((e.time_et || "").slice(11, 16))}</b><span>美东 ${esc((e.time_et || "").slice(5, 10))}</span><span>本地 ${fmtLocalHM.format(d)}</span>`;
  const first = (e.links || [])[0];
  const title = first ? `<a class="hl" href="${esc(first.url)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>` : `<span class="hl">${esc(e.title)}</span>`;
  const why = [...(e.reasons || []), e.l2_reason ? `智能分类：${e.l2_reason}` : ""].filter(Boolean).join("；");
  const sec = (e.sec_items || []).map((x) => `Item ${esc(x.item)} ${esc(x.zh)}`).join(" · ");
  const links = (e.links || []).map((l) => `<a class="lnk" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer" title="${esc(l.title)}">${esc(l.source)}</a>`).join("");
  return `<div class="tl" data-event="${esc(e.id)}">
    <div class="when">${when}</div>
    <div class="body">
      <div class="meta"><span class="lv ${LV_CLS[e.level] || "l"}" title="${esc(why || "无关键词命中")}">${esc(e.level)}</span>
        <span>${esc((e.sources || []).join(" · "))}</span>${e.count > 1 ? `<span>${e.count} 条相关报道已合并</span>` : ""}</div>
      ${title}
      ${e.summary_zh ? `<div class="zh">${esc(e.summary_zh)}</div>` : ""}
      ${sec ? `<div class="meta">${sec}</div>` : ""}
      ${(e.links || []).length > 1 ? `<div class="links">${links}</div>` : ""}
    </div></div>`;
}

function renderNews() {
  renderNewsFilters();
  const st = S.news.settings || {};
  const secOn = st.sources && st.sources.sec && st.sources.sec.enabled;
  $("#secEmailRow").hidden = !(secOn && !st.contact_email && !st.mock);
  const l2 = st.layer2 || {};
  $("#cap4").textContent = `按重要性 高 / 中 / 低 打分（关键词规则${l2.enabled ? " + 智能分类" : ""}）· 同一事件的多条报道会合并 · 链接在新标签页打开`;
  const ev = S.news.events || [];
  setHTML($("#newsTimeline"), ev.length ? ev.map(eventHTML).join("") : `<div class="empty">暂无符合筛选条件的新闻。资讯来源每 1 到 5 分钟轮询一次。</div>`);
  const er = S.news.earnings || [];
  setHTML($("#earnPanel"), er.length ? er.map((r) => `<div class="mrow"><span class="nm"><span class="code">${esc(short(r.code))}</span><small>${esc(r.name || "")}</small></span>
      <span class="v">${esc((r.date || "").slice(5))} ${esc(r.pub_zh)}</span><span class="m">${r.time_et ? "美东 " + esc(r.time_et.slice(11, 16)) : ""} ${esc(r.period || "")}</span></div>`).join("")
    : `<div class="empty">未来 7 天自选标的没有财报。</div>`);
}

function mktSeen() { return +(localStorage.getItem("mktSeen") || 0); }
function updateBadge() {
  const el = $("#mktBadge");
  el.hidden = !S.badge;
  el.textContent = S.badge > 99 ? "99+" : String(S.badge);
}
function onNewsAlert(e) {
  const where = (e.symbols || []).map((s) => (s.startsWith("KW:") ? s.slice(3) : short(s))).join("、");
  toast("alert", `${e.level}重要性新闻（${where}）：${e.title}`);
  beep();
  if (e.level === "高" && localStorage.getItem("tab") !== "market") { S.badge += 1; updateBadge(); }
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(`${e.level}重要性新闻 · ${where}`, { body: e.summary_zh || e.title, tag: "news:" + e.id }); } catch { /* */ }
  }
  for (const s of e.symbols || []) $$(`tr[data-code="${s}"]`).forEach(flash);
  scheduleNews();
}

// settings sheet (资讯来源 / SEC 邮箱 / 关键词 / 智能分类 / 通知)
async function openSettings(trigger, focusEmail = false) {
  let st;
  try { st = await api("GET", "/api/news/settings"); } catch (e) { toast("error", e.message); return; }
  const srcRows = [["moomoo_search", "moomoo 资讯搜索", "官方 News Search 接口，每个标的每 2 分钟 1 次"],
    ["moomoo_api", "moomoo API 资讯", "通过 OpenD 搜索资讯与公告（每 30 秒最多 10 次）"],
    ["sec", "SEC EDGAR", "8-K、6-K、13D、13G 文件，每 60 秒，最多每秒 5 次"],
    ["earnings", "财报日历", "通过 OpenD 获取未来 7 天财报"]];
  const tg = (id, on) => `<button class="tg" type="button" role="switch" aria-checked="${!!on}" data-tg="${id}"></button>`;
  const l2 = st.layer2 || {};
  const sh = openSheet(`<h3 id="sheetTitle">设置</h3>
    <div class="form">
      <div class="field"><span>资讯来源</span><div class="group">${srcRows.map(([k, label, hint]) => `<div class="set-row"><div class="lbl">${label}<small>${hint}</small></div>${tg("src_" + k, st.sources[k] && st.sources[k].enabled)}</div>`).join("")}
        ${st.mock ? `<div class="hint">模拟模式不访问网络，使用模拟资讯与模拟 SEC 文件。</div>` : ""}</div></div>
      <label class="field"><span>SEC 联系邮箱（SEC 要求 User-Agent 包含名称和联系邮箱；只保存在本机配置文件）</span>
        <input class="input" id="setEmail" type="email" placeholder="name@example.com" value="${esc(st.contact_email || "")}"></label>
      <label class="field"><span>额外关注的关键词（每行一个，除自选标的外也会搜索）</span><textarea class="input" id="setExtra">${esc((st.extra_keywords || []).join("\n"))}</textarea></label>
      <label class="field"><span>标的英文名称（用于资讯搜索。BE 这类短代码会搜到无关新闻，填写公司名更准确；每行一个，例如 US.BE = Bloom Energy）</span>
        <textarea class="input" id="setAlias">${esc(Object.entries(st.symbol_aliases || {}).map(([k, v]) => `${k} = ${v}`).join("\n"))}</textarea>
        <span class="hint">当前搜索词：${esc(Object.entries(st.search_keywords || {}).map(([k, v]) => `${short(k)} → ${v}`).join("；") || "—")}</span></label>
      <label class="field"><span>高重要性关键词（每行一个，中英文均可）</span><textarea class="input" id="setHigh" style="min-height:110px">${esc(st.keywords.high.join("\n"))}</textarea></label>
      <label class="field"><span>中重要性关键词</span><textarea class="input" id="setMed">${esc(st.keywords.medium.join("\n"))}</textarea></label>
      <div class="field"><span>智能分类（Claude，可选）</span><div class="group">
        <div class="set-row"><div class="lbl">启用智能分类<small>模型 ${esc(l2.model || "")} · 今日调用 ${l2.used_today ?? 0} / ${l2.daily_cap ?? 0} · ${l2.key_present ? "已检测到 ANTHROPIC_API_KEY" : "未检测到环境变量 ANTHROPIC_API_KEY"}</small></div>${tg("layer2", l2.enabled)}</div>
        <div class="hint">只发送标题、来源、时间和摘要前 500 字；密钥只从环境变量读取，不会写入文件。达到每日上限后自动回退到关键词规则。</div>
        ${l2.notice ? `<div class="warn-text">${esc(l2.notice)}</div>` : ""}${l2.last_error ? `<div class="warn-text">最近错误：${esc(l2.last_error)}</div>` : ""}</div></div>
      <div class="field"><span>通知</span><div class="group"><div class="set-row"><div class="lbl">中重要性新闻也通知<small>高重要性新闻总是弹出桌面通知并播放提示音</small></div>${tg("notify_medium", st.notify_medium)}</div></div></div>
      <div class="err" id="setErr"></div>
    </div>
    <div class="acts"><button class="primary" id="setSave">保存</button><button class="ghost" data-close>取消</button></div>`, trigger);
  $$("[data-tg]", sh).forEach((b) => (b.onclick = () => b.setAttribute("aria-checked", b.getAttribute("aria-checked") === "true" ? "false" : "true")));
  if (focusEmail) setTimeout(() => $("#setEmail", sh).focus(), 80);
  const lines = (id) => $(id, sh).value.split(/[\n,，]+/).map((x) => x.trim()).filter(Boolean);
  const on = (id) => $(`[data-tg="${id}"]`, sh).getAttribute("aria-checked") === "true";
  $("#setSave", sh).onclick = async () => {
    const body = {
      sources: Object.fromEntries(srcRows.map(([k]) => [k, { enabled: on("src_" + k) }])),
      contact_email: $("#setEmail", sh).value.trim(), extra_keywords: lines("#setExtra"),
      symbol_aliases: Object.fromEntries($("#setAlias", sh).value.split("\n").map((l) => l.split(/[=＝:：]/))
        .filter((x) => x.length >= 2 && x[0].trim() && x.slice(1).join("=").trim())
        .map((x) => [x[0].trim().toUpperCase(), x.slice(1).join("=").trim()])),
      keywords: { high: lines("#setHigh"), medium: lines("#setMed") }, notify_medium: on("notify_medium"), layer2_enabled: on("layer2"),
    };
    try { S.news.settings = await api("PUT", "/api/news/settings", body); closeSheet(); toast("info", "设置已保存"); renderNews(); }
    catch (e) { $("#setErr", sh).textContent = e.message; }
  };
}

$("#btnSettings").addEventListener("click", (e) => openSettings(e.currentTarget));
$("#btnSecEmail").addEventListener("click", (e) => openSettings(e.currentTarget, true));
$("#btnMktRefresh").addEventListener("click", () => run(api("POST", "/api/market/refresh").then((r) => toast("info", r.message))));
$("#plateSet").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-ps]");
  if (!b || (S.market && S.market.plate_set === b.dataset.ps)) return;
  S.mktExpanded = null;
  run(api("POST", "/api/market/plate_set", { plate_set: b.dataset.ps }).then((m) => { S.market = m; renderMarket(); }));
});
["#minCap", "#minDv"].forEach((sel) => $(sel).addEventListener("change", () => {
  run(api("PUT", "/api/market/size_filter", { min_market_cap_usd: Number($("#minCap").value) * 1e8, min_dollar_volume_usd: Number($("#minDv").value) * 1e4 })
    .then((m) => { S.market = m; renderMarket(); }));
}));
$("#newsLevel").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-lv]");
  if (!b) return;
  S.newsFilter.levels = b.dataset.lv;
  renderNewsFilters();
  loadNews().catch((er) => toast("error", er.message));
});
$("#newsSymbol").addEventListener("change", (e) => { S.newsFilter.symbol = e.target.value; loadNews().catch(() => {}); });
$("#newsSource").addEventListener("change", (e) => { S.newsFilter.source = e.target.value; loadNews().catch(() => {}); });
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-act]");
  if (!t) return;
  if (t.dataset.act === "sector") {
    const c = t.dataset.code, key = `${t.dataset.list}|${c}`;
    S.mktExpanded = S.mktExpanded === key ? null : key;
    ["#gainers", "#losers", "#inflow", "#outflow"].forEach((s) => ($(s)._html = null));
    renderMarket();
    if (S.mktExpanded) loadSector(c);
  } else if (t.dataset.act === "bench-edit") {
    openBenchEditor(t);
  }
});

// ─────────────────────────────────────────────────────────────── boot + websocket
function renderAll() {
  renderStatus(); renderStocks(); renderOptions(); renderAlerts(); renderTrading(); renderNotifyRow();
  renderMarket(); renderNews(); updateBadge();
}

async function boot() {
  const st = await api("GET", "/api/state");
  S.mode = st.mode;
  S.quotes = st.quotes || {};
  S.watchlist = st.watchlist || [];
  setSubs(st.subs);
  S.status = st.status;
  S.options = st.options;
  S.chains = {};
  for (const m of st.chains || []) S.chains[ckey(m.underlying, m.expiry)] = m;
  S.alerts = st.alerts;
  S.trading = st.trading;
  S.condTypes = st.condition_types;
  S.stockSubtypes = st.stock_subtypes;
  S.holdSec = st.hold_sec || 60;
  S.market = st.market;
  S.news = { events: (st.news && st.news.events) || [], earnings: (st.news && st.news.earnings) || [],
    settings: st.news && st.news.settings };
  setAnalysisSettings(st.analysis);
  const anOn = !!(st.analysis && st.analysis.enabled);
  $('#tabs button[data-tab="analysis"]').hidden = !anOn;
  if (anOn && wantTab === "analysis") showTab("analysis");
  else if (!anOn && !$("#view-analysis").hidden) showTab("stocks");
  wantTab = null;
  const seen = mktSeen();
  S.badge = localStorage.getItem("tab") === "market" ? 0
    : S.news.events.filter((e) => e.level === "高" && e.published > seen && Date.now() / 1000 - e.published < 86400).length;
  ["#benchStrip", "#gainers", "#losers", "#inflow", "#outflow", "#leaderTable tbody", "#newsTimeline", "#earnPanel", "#newsRow"]
    .forEach((x) => { const el = $(x); if (el) el._html = null; });
  for (const el of $$("[data-chain]")) el.remove();
  ["#statusGrid", "#stockTable tbody", "#pinTable tbody", "#underlyings", "#ruleTable tbody", "#alertHistory",
    "#tradeSwitches", "#tradeToday", "#tradeLimits", "#posTable tbody", "#orderTable tbody", "#logList"].forEach((s) => { const el = $(s); if (el) el._html = null; });
  renderAll();
  for (const u of S.options.underlyings || []) loadExps(u.code);
  loadNews().catch(() => {});  // keep the user's 高/中/全部 filter across reconnects
}

function handle(m) {
  const d = m.data;
  switch (m.type) {
    case "quotes":
      for (const [c, r] of Object.entries(d)) { S.quotes[c] = r; applyQuote(c, r); }
      for (const c of Object.keys(d)) onUnderlyingMove(c);
      remarkChains(d);
      break;
    case "status": S.status = d; renderStatus(); break;
    case "quota": if (S.status) { S.status.quota = d; renderStatus(); } break;
    case "subs":
      setSubs(d); $("#stockTable tbody")._html = null; renderStocks();
      for (const k of Object.keys(S.chains)) renderChain(k);
      break;
    case "watchlist": S.watchlist = d; renderStocks(); renderNewsFilters(); break;
    case "options_config": S.options = d; renderOptions(); break;
    case "chain": S.chains[ckey(d.underlying, d.expiry)] = d; renderChain(ckey(d.underlying, d.expiry)); break;
    case "alerts": S.alerts = d; renderAlerts(); break;
    case "alert": onAlert(d); break;
    case "trading": S.trading = d; renderTrading(); renderAlerts(); break;
    case "toast": toast(d.level, d.text); break;
    case "market": S.market = d; renderMarket(); break;
    case "news_changed": scheduleNews(); break;
    case "news_alert": onNewsAlert(d); break;
    case "earnings": scheduleNews(); break;
    case "news_settings": S.news.settings = d; renderNews(); break;
    case "analysis_progress": onAnalysisProgress(d); break;
    case "book": S.details[d.code] = { ...(S.details[d.code] || {}), book: d }; renderDetail(d.code); break;
    case "ticks": S.details[d.code] = { ...(S.details[d.code] || {}), ticks: d.ticks }; renderDetail(d.code); break;
    default: break;
  }
}

let ws = null, pingTimer = null;
function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    setNavConn(true);
    boot().catch((e) => toast("error", e.message));
    pingTimer = setInterval(() => { try { ws.send("ping"); } catch { /* */ } }, 20000);
  };
  ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => { setNavConn(false); clearInterval(pingTimer); setTimeout(connectWS, 2000); };
}

setInterval(tickClock, 1000);
setInterval(() => { for (const c of S.expanded) loadDetail(c); }, 5000);
connectWS();
