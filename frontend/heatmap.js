// 市场热力图 tab: TradingView's official Stock Heatmap embed widget (S&P 500, colored by change,
// sized by market cap, grouped by sector). No iframe of tradingview.com is created here; the widget script
// builds its own. The script element is created on mount (JSON config in script.innerHTML) and the
// container is emptied on unmount, so switching tabs never leaves two heatmaps.
export const HEATMAP_PAGE = "https://www.tradingview.com/heatmap/stock/#%7B%22dataSource%22%3A%22SPX500%22%2C%22blockColor%22%3A%22change%22%2C%22blockSize%22%3A%22market_cap_basic%22%2C%22grouping%22%3A%22sector%22%7D";
const SRC = "https://s3.tradingview.com/external-embedding/embed-widget-stock-heatmap.js";

const config = (colorTheme) => ({
  exchanges: [],
  dataSource: "SPX500",
  grouping: "sector",
  blockSize: "market_cap_basic",
  blockColor: "change",
  locale: "en",
  symbolUrl: "",
  colorTheme,
  hasTopBar: true,
  isDataSetEnabled: true,
  isZoomEnabled: true,
  hasSymbolTooltip: true,
  width: "100%",
  height: "100%",
});

// The dashboard's current mode, read from what is actually rendered (works with a data-theme
// attribute, a class or a media query): a dark page background means dark.
export function dashboardTheme() {
  const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(document.body).backgroundColor || "");
  if (!m) return "light";
  const [r, g, b] = m.slice(1, 4).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5 ? "dark" : "light";
}

let host = null, mountedTheme = null;

export function mountHeatmap(el) {
  unmountHeatmap();
  host = el;
  mountedTheme = dashboardTheme();
  const box = document.createElement("div");
  box.className = "tradingview-widget-container";
  box.style.height = "100%";
  box.style.width = "100%";
  const widget = document.createElement("div");
  widget.className = "tradingview-widget-container__widget";
  widget.style.height = "calc(100% - 32px)";   // leaves a 32px line for the TradingView copyright link
  widget.style.width = "100%";
  const copy = document.createElement("div");
  copy.className = "tradingview-widget-copyright";
  copy.innerHTML = '<a href="https://www.tradingview.com/heatmap/stock/" rel="noopener nofollow" target="_blank"><span class="blue-text">Stock Heatmap</span></a><span class="trademark"> by TradingView</span>';
  const script = document.createElement("script");
  script.type = "text/javascript";
  script.src = SRC;
  script.async = true;
  script.innerHTML = JSON.stringify(config(mountedTheme), null, 2);
  box.append(widget, copy, script);
  el.replaceChildren(box);
}

export function unmountHeatmap() {
  if (host) host.replaceChildren();
  host = null;
  mountedTheme = null;
}

// Reload when the dashboard switches between light and dark.
function recheck() {
  if (host && dashboardTheme() !== mountedTheme) mountHeatmap(host);
}
const mo = new MutationObserver(recheck);
mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
mo.observe(document.body, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
try { matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => setTimeout(recheck, 50)); } catch { /* old browsers */ }
