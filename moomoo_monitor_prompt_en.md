# Build a local moomoo stock and options monitoring dashboard

## Task 1 (do this first): install moomoo OpenD on this machine

Before any other work, install OpenD, the local gateway that the moomoo API requires. Do not start the dashboard code until this task is finished and the user has confirmed that OpenD is logged in.

Detect the operating system and CPU architecture of this machine first. Then follow the official setup guide for AI coding agents at https://openapi.moomoo.com/moomoo-api-doc/en/intro/ai.html step by step. If that guide cannot be followed on this machine, download the matching OpenD package from the official download page https://www.moomoo.com/download/OpenAPI and install it. Download only from moomoo.com or openapi.moomoo.com; never use a mirror or a third party copy. The download page currently lists OpenD version 10.11.7108, so install that version or newer.

The download page offers two editions: the GUI edition (Visualization OpenD) and the command line edition. Install the GUI edition by default, because the user must log in and unlock trading by hand, which is simplest in the GUI. Tell the user where it was installed and how to launch it.

Install the Python SDK in a project virtual environment. The download page states the install command is `pip install moomoo-api`; confirm the package name against the official SDK README and make sure the SDK version matches the installed OpenD version.

Never type, store, log or autofill the user's moomoo login password or trading password, and never put them in a config file, an environment file or a command line argument. moomoo's own terms for AI agents require the user to enter the password manually in OpenD. When OpenD is installed, stop and ask the user to do three things by hand: launch OpenD and log in with their moomoo account; complete the API questionnaire and agreements shown at first login; check that the US options quote right shows LV1. LV1 for US options is free when total account assets are above zero or the account holds US positions; otherwise the user must buy the OPRA options quotation card that is marked as applicable to OpenD.

Confirm that OpenD listens on 127.0.0.1 only, on the default port 11111. Then run a short connection test with the SDK: open a quote context, request one market snapshot for `US.QQQ`, print the result, and close the context. Only continue to the next task when this test succeeds and the user confirms the login.

## Task 2: verify the official documentation before writing code

The moomoo API changes between versions, so read the official sources and confirm every function signature, parameter name, enum value and rate limit you use. If anything in this prompt conflicts with the official documentation, follow the documentation and list the difference in the README.

Official documentation: https://openapi.moomoo.com/moomoo-api-doc/en/
Official Python SDK: https://github.com/MoomooOpen/py-moomoo-api
Official API quick reference: `Moomoo API Skills/moomooapi/docs/API_REFERENCE.md` in https://github.com/MoomooOpen/moomoo-agent-hub

Put every rate limit and quota value in one configuration file so it can be edited later without touching code. Values taken from documentation v10.12 for your verification: the market snapshot endpoint allows at most 60 requests per 30 seconds and at most 400 codes per request; the option chain endpoint allows at most 10 requests per 30 seconds and a date span of at most 30 days per request; the place order endpoint allows at most 15 requests per 30 seconds, and combo orders share that same budget.

## Task 3: build the dashboard

### Goal

Build a general purpose monitoring dashboard that runs only on this machine and uses the moomoo OpenAPI as its data source. It must cover US stocks (including ETFs) and US options. The user adds or removes any symbol, any expiration date and any option contract from the UI at any time, so nothing may be hardcoded to a specific day or ticker. Automatic order placement is an optional feature behind a master switch that is OFF by default.

All user facing text in the dashboard must be in Simplified Chinese: labels, buttons, table headers, tooltips, alert messages, error messages and the README section titled 使用说明. Source code, comments, log files and the rest of the README may be in English.

The dashboard backend must bind to 127.0.0.1 only.

### Architecture

Backend: Python with FastAPI. The backend owns one quote context (`OpenQuoteContext`) and one trade context (`OpenSecTradeContext`). Every call to OpenD goes through the backend; the frontend never talks to OpenD. The backend pushes quote updates, quota status and alert events to the frontend over a WebSocket.

Frontend: a local web page served by the backend. Framework is your choice. The visual design must follow the design skill described in the Design section below.

Mock mode: add a startup flag that runs the backend without OpenD and generates simulated quotes, so the UI can be built and tested when the market is closed.

### Feature 1: stock and ETF monitor

The user types any US symbol in moomoo format (for example `US.QQQ` or `US.NVDA`) to add it to a watchlist, and can remove it at any time. For each symbol the user picks which subscription types are active. Offer at least: quote, ticker (trade by trade), order book, 1 minute candles and 5 minute candles. Use the exact subscription type names from the official docs. Checking or unchecking a type calls subscribe or unsubscribe immediately.

Respect two subscription rules and surface them in the UI. Each subscription type for each stock consumes 1 subscription quota. A subscription must stay active for at least one minute before it can be cancelled; if the user tries to cancel sooner, show a clear Chinese message with the remaining wait time and schedule the unsubscribe automatically.

Each watchlist row shows last price, change, percent change, day high, day low, volume and the close of the most recent 5 minute candle. If the API returns premarket, after hours or overnight prices, show them and label the session.

### Feature 2: options monitor

For any underlying the user enters, the backend first fetches the list of expiration dates, then fetches the option chain for each expiration the user selects. The chain endpoint returns static contract data only; quotes come from subscription or snapshot. Fetch each chain once (at startup, at the first request, or on a manual refresh button) and cache it locally for the day, because the chain endpoint has the tightest rate limit. Open interest for US options updates once a day during the premarket session, so a daily cache is sufficient.

Display each expiration as a straddle table: strike in the center column, calls on the left, puts on the right. Each contract shows bid, ask, mid, last, volume, open interest and implied volatility, plus Greeks if the snapshot returns them. Highlight the strike closest to the underlying's last price, and let the user limit the view to N strikes above and below that strike, with N adjustable.

For each expiration the user chooses one of two quote modes. Subscription mode: subscribing one data type for one option chain (all contracts with the same expiration) consumes 1 option subscription quota, and the option quota pool is separate from the stock quota pool. Snapshot polling mode: consumes no subscription quota; polling interval is adjustable with a default of 5 seconds and a minimum of 1 second; batch up to 400 codes per request and obey the snapshot rate limit.

Provide a separate options watchlist where the user pins individual contracts they intend to trade. It displays apart from the chain tables.

### Feature 3: status and quota panel

Keep a persistent status area that shows: OpenD connection state; current quote right level for US stocks and US options; stock subscription quota used and remaining; option subscription quota used and remaining; and, for every rate limited endpoint, the number of calls used in the current 30 second window. Read remaining quota from the official subscription status query.

Before any new subscription, check that quota is available. If it is not, reject the action in the UI with a Chinese explanation, and do not send the request.

Only one terminal per account receives the highest quote right. If the user opens the moomoo app on a phone, OpenD may lose it. Detect this condition, show it prominently, and provide a button that requests the highest quote right again through the official command or endpoint.

US index quotes are not supported by the API, so VIX and the Nasdaq 100 index cannot be monitored directly. If the user enters an index code, tell them in Chinese to use the corresponding ETF.

### Feature 4: alert rules

The user creates alert rules for any stock or option contract. Support at least these condition types: last price above or below a value; 5 minute candle close above or below a value; price inside a range; option mid price above or below a value; option bid ask spread wider than a value. Conditions can be combined with AND, for example: QQQ inside 674.5 to 676 AND 5 minute close not below 674. Each rule can be limited to a time window in US Eastern time, for example 09:30 to 10:30.

When a rule fires, highlight the affected row, send a desktop notification and play a sound. Each rule is either fire once or fire again after a cooldown. Rules persist in a local config file and reload on restart.

All market logic uses America/New_York time. Display Eastern time and the user's local time in the status area.

### Feature 5: automatic orders behind a switch

Automatic ordering is OFF by default. A master switch in the UI enables it; only when it is on can a rule be linked to an order action. A second, independent switch selects the environment: paper trading (`TrdEnv.SIMULATE`, the default) or live trading. Switching to live trading requires a two step confirmation in the UI, and the user must unlock trading manually in the OpenD GUI. The code must never unlock trading with a stored password.

Order actions: buy or sell a specified contract, or submit a spread as one combo order. Combo order support and allowed leg sides can differ by the broker entity the account belongs to, so verify combo orders in paper trading first and disable the combo option in the UI if the account does not support it.

Limit orders only. The default limit is the mid price at the moment of submission, with a user configurable offset from mid. The following hard limits live in the config file; the UI shows them, and changing them requires confirmation: maximum contracts per order; maximum orders per day; maximum daily loss, after which the automatic ordering switch turns itself off; stop conditions and a time stop (close the position at a set time if the target has not been reached).

Show a prominent emergency stop button. Pressing it cancels all open orders and switches automatic ordering off at once.

Write every order, modification, cancellation and fill report to a local log file with timestamp, contract, side, quantity, price, triggering rule and the raw API response.

### Rate limiting

Route every OpenD request through one rate limiter that keeps a sliding 30 second window for each endpoint. When a request would exceed the limit, queue it until capacity is available; never send it and wait for an error. Order requests have priority over snapshot polling, so orders still go out quickly when polling is busy.

### Reliability

If OpenD disconnects, reconnect automatically and restore every previous subscription. On shutdown, unsubscribe and close both contexts so quota is released. Keep a runtime log for troubleshooting.

### Design

Use the Apple Liquid Glass design skill in `./design/apple_liquid_glass/`. Before building any UI, read `SKILL.md` and follow its Build mode workflow in order: read `design-system.md`, use `tokens.css` for every color, radius, shadow and spacing value, pick a recipe from `patterns.md`, compose from `components.md`, read `motion.md` for dialogs, popovers and confirmations, read `icons.md` before adding icons, compare against `reference.html`, and pass `checklist.md` before calling the UI done. The skill is MIT licensed; keep its LICENSE file in the folder.

Apply these project specific overrides to the skill, and state them in the README:

Price direction colors are an explicit exception to the skill's grayscale rule. Use green for up and red for down on price change, percent change and profit and loss values only (green means up, red means down). Everything else stays grayscale with the single blue accent defined by the skill.

This is a data dense dashboard, so the 1080 px grid container may be too narrow. A wider max width (around 1440 px) is acceptable for the main monitor view, but keep the skill's unified white panel with hairline dividers for every table, and keep glass only on the top navigation bar, popovers and modal dialogs.

All numbers use `tabular-nums` so columns of prices stay aligned while they update. Use the system font stack from the skill, which includes PingFang for Chinese text.

### Configuration

Use one YAML or JSON config file for: OpenD host and port; rate limit and quota values; the watchlist and the subscription types for each symbol; quote mode and polling interval for each option expiration; alert rules; automatic order hard limits. Example rule shape (field names are your choice):

```yaml
alerts:
  - name: QQQ gap fill long
    symbol: US.QQQ
    active_window_et: ["09:30", "10:30"]
    conditions:
      - type: price_in_range
        low: 674.5
        high: 676.0
      - type: kline_5m_close_above
        value: 674.0
    trigger_once: true
    order_action:
      enabled: false
      side: BUY
      contract: chosen by the user in the UI
      quantity: 1
      limit_price: mid
```

### Deliverables and acceptance

Deliver the full source code, a dependency file and a README. The README includes a Chinese section titled 使用说明 that walks the user through: installing OpenD; logging in and completing the API questionnaire; confirming LV1 for US options; installing dependencies and starting the backend; opening the dashboard in a browser.

Acceptance runs in this order. First, in mock mode, confirm every UI feature works. Second, connected to OpenD during market hours, confirm stock push updates, the option chain table, both option quote modes and the quota display. Third, in paper trading, run one test rule end to end through trigger, order, stop and emergency stop, and confirm the log is complete. Do not switch the environment to live trading until all three steps pass.

Write unit tests for the alert rule engine and the rate limiter.
