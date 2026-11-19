# Add a market overview page (sectors, capital flow, sector leaders, news)

## Goal

Add a new tab named 市场 to the top navigation of the existing dashboard. Place it between 期权 and 提醒, so the tab order becomes: 股票, 期权, 市场, 提醒, 自动交易. The page answers four questions about the current US session, in this order from top to bottom:

1. Which sectors are up the most and which are down the most today.
2. Which sectors have the largest net capital inflow and the largest net outflow.
3. Which stock is the top gainer in each sector.
4. Which news items today are important.

Keep every existing feature unchanged. All user facing text is Simplified Chinese. The UI follows the design skill in `./design/apple_liquid_glass/` like the rest of the dashboard: one unified white panel with hairline dividers for each section, grayscale with the single blue accent, green for up and red for down on price change and capital flow values, `tabular-nums` for every number.

The news section is specified in `news_monitor_addon_prompt_en.md`, which is provided with this prompt. Implement it with one change: the news timeline is the fourth section of the 市场 page instead of a separate 资讯 tab, and the count badge for high importance news goes on the 市场 tab. If a 资讯 tab already exists from that earlier prompt, move its content into this page and remove the separate tab.

## Step 1: verify the moomoo functions before writing code

Read the official documentation and `Moomoo API Skills/moomooapi/docs/API_REFERENCE.md` in https://github.com/MoomooOpen/moomoo-agent-hub, and confirm each point below by running a small test against the live OpenD. Record every result and every rate limit in the config file and the README.

Confirm that `get_plate_list` returns US plates, and which plate set types exist for the US market (for example industry and concept). Confirm the rate limit of `get_plate_list` and of `get_plate_stock`.

Confirm whether `get_market_snapshot` accepts plate codes and returns a change rate for a plate. If it does, use the plate snapshot as the sector change. If it does not, compute the sector change yourself as described in Step 2.

Confirm whether `get_capital_flow` accepts a plate code. Confirm its rate limit. The documentation states its output covers regular trading hours only, not premarket or after hours; show that note in the UI.

## Step 2: data method

Sector universe. Use the US industry plate set by default. Add a segmented control on the page so the user can switch between industry plates and concept plates. Fetch the plate list and every plate's constituents once per day, spread over time to respect the rate limits, and cache them locally. Add a manual refresh button.

Sector change. If plate snapshots provide a change rate, use it directly. Otherwise compute it from constituent snapshots as a market cap weighted average of the constituents' percent change. Label the method in a small grey caption under the section title, so the user knows whether the number comes from moomoo directly or is computed.

Sector capital flow. If `get_capital_flow` accepts plate codes, use the plate level net inflow. If it does not, sum the net inflow of the constituents that pass the size filter below, limited to what the rate limit allows within a 5 minute refresh cycle; if even that is not possible within the limit, use the sector ETF as a proxy (for example XLK for technology, XLF for financials, XLE for energy, SMH or SOXX for semiconductors) and label it clearly as an ETF proxy. Show values in US dollars with 万 and 亿 units, consistent with the volume column on the 股票 page.

Sector leaders. From constituent snapshots, find the top gainer in each sector. Apply a size filter so illiquid micro caps do not dominate: market cap at least 2 billion US dollars and dollar volume today at least 20 million US dollars by default, both editable on the page. For each leader show code, name, percent change, last price and dollar volume. If the news section has an item about that stock from the last 24 hours, show its headline as a one line catalyst under the stock, linked to the source.

Benchmark strip. At the top of the page, show a compact row of benchmark ETFs with last price and percent change. Default list: SPY, QQQ, TQQQ, DIA, SOXX. The user can edit the list.

Refresh schedule. During regular hours, refresh sector change, leaders and the benchmark strip every 60 seconds, and capital flow every 5 minutes. Outside regular hours, keep the last session's numbers on screen and show a label with the session date and time of the last update. All refresh intervals are editable in the config file.

Budget. This page uses snapshots only and must not consume subscription quota. It must go through the existing rate limiter and use at most half of the snapshot budget, so the 股票 and 期权 pages and any order request are never delayed by it. Show the budget used by this page in the existing status panel.

## Step 3: page layout

Section 1, 板块涨跌. Two lists side by side on wide screens (one column on narrow screens): the 10 sectors with the largest gains on the left, the 10 with the largest losses on the right. Each row shows sector name, percent change, and the number of constituents up and down. Clicking a sector expands it inline to show its constituents sorted by percent change.

Section 2, 资金流向. Two lists side by side: the 10 sectors with the largest net inflow and the 10 with the largest net outflow. Each row shows sector name, net flow amount and the sector's percent change.

Section 3, 板块领涨. One table with one row per sector, sorted by the leader's percent change, showing sector name, leader code and name, leader percent change, last price, dollar volume, and the catalyst headline if one exists.

Section 4, 重要新闻. The news timeline from `news_monitor_addon_prompt_en.md`, filtered by default to 高 and 中 importance, with the existing filters.

Each section title has a grey caption with the time of the last update and the data method.

## Step 4: testing and documentation

In mock mode, generate simulated plates, constituents, capital flows and news so the whole page works when OpenD is not connected. Write unit tests for the weighted sector change, the size filter for leaders, and the budget split in the rate limiter. Update the README section 使用说明 to describe the page, the data method chosen after Step 1, and any limitation found. List any difference between this prompt and the official documentation in the README.
