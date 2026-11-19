# Add a news and event monitor to the existing dashboard

## Goal

Add a new page named 资讯 to the existing dashboard in this project. It watches important news, company announcements and regulatory filings for every symbol in the watchlist, plus any keywords the user adds, and raises alerts for the important ones. Examples of events it must catch: a federal investigation of a company executive, an earnings release, a guidance change, an export control decision.

Keep every existing feature unchanged. All user facing text stays in Simplified Chinese. The UI follows the design skill in `./design/apple_liquid_glass/`, the same way as the rest of the dashboard.

## Step 1: verify each source before writing code

Read the official documentation of every source below, confirm endpoints, parameters, terms of use and rate limits, and put every limit in the config file. If a source's terms do not allow automated polling, leave that source disabled and say so in the README.

Source A, moomoo News Search skill. It is an official moomoo skill in https://github.com/MoomooOpen/moomoo-agent-hub, with an install guide at https://www.moomoo.com/skills/news-search-install.md. According to the repository README, it calls moomoo public HTTP content APIs and does not need OpenD. It searches news, announcements and research reports. Read the skill files to find the endpoint and the response format. If no rate limit is documented, poll conservatively: one request per watchlist symbol every 2 minutes, spread evenly, and make the interval configurable.

Source B, moomoo API news and announcement functions. The official `moomooapi` skill lists news and announcement search among its capabilities. Check `Moomoo API Skills/moomooapi/docs/API_REFERENCE.md` for the exact functions. Use them only if they exist in the installed SDK version.

Source C, SEC EDGAR filings. Use the official submissions API (`https://data.sec.gov/submissions/CIK##########.json`, with the CIK zero padded to 10 digits) for each watchlist company, and map tickers to CIK with the official `company_tickers.json` file. SEC fair access rules require a User-Agent header with a name and a contact email; ask the user for the email on first setup, store it in the config file, and never use a library default User-Agent. Stay at or below 5 requests per second in total, well under the official cap of 10 per second. Poll every 60 seconds. Watch these forms: 8-K (record the item numbers, because item 2.02 is an earnings release, item 5.02 is an officer or director change, item 1.05 is a cybersecurity incident and item 8.01 covers other events), 6-K for foreign issuers, and SC 13D and SC 13G. Form 4 insider filings are optional and off by default.

Source D, earnings calendar. If the moomoo API provides an earnings date function, show upcoming earnings for every watchlist symbol within the next 7 days, including whether the release is before the open or after the close when the data provides it. If no such function exists, derive what you can from Source A and state the limitation in the README.

## Step 2: importance scoring

Score every item as 高, 中 or 低 with two layers.

Layer 1 is a rule engine and is always on. It uses keyword lists that the user can edit in the UI, in English and Chinese. Default high importance keywords include: FBI, DOJ, SEC investigation, subpoena, indictment, probe, export control, Commerce Department, Entity List, recall, FDA approval, FDA rejection, guidance cut, guidance raise, resigns, steps down, acquisition, merger, bankruptcy, trading halt, short seller report, 调查, 起诉, 出口管制, 下调指引, 上调指引, 辞职, 收购, 破产, 停牌. An SEC 8-K with item 2.02, 5.02 or 1.05 is always 高. An item that mentions a watchlist symbol in the headline ranks higher than one that mentions it only in the body.

Layer 2 is an optional classifier that uses the Anthropic API. It is OFF by default and has its own switch in the settings page. It reads the API key only from the environment variable `ANTHROPIC_API_KEY`; never store the key in a file. Send only the headline, the source, the timestamp and at most the first 500 characters of the summary. Ask for JSON output with these fields: importance (高, 中 or 低), affected symbols, a short Chinese summary of one sentence, and the reason for the score in one sentence. Check the Anthropic documentation for the current model names and put the model name in the config file. Add a daily call cap in the config file (default 300) and show the number of calls used today on the settings page. When the cap is reached, fall back to Layer 1 only and show a notice.

## Step 3: deduplication and storage

The same event often appears in several outlets and in a filing. Group items that refer to the same event: same symbol, publication times within 30 minutes, and similar normalized headlines. Show one entry per event with a count of sources and a list of links. Store all items in a local SQLite database for 30 days, then delete them automatically.

## Step 4: UI

The 资讯 page shows a timeline in one unified panel with hairline dividers, newest first. Each entry shows: Eastern time and local time, source names, the original headline in its original language, the Chinese one sentence summary when Layer 2 is on, the importance level, the affected symbols, and links that open the original pages in a new browser tab. Provide filters for symbol, importance level and source.

When a 高 item arrives, send a desktop notification, play a sound, and show a count badge on the 资讯 entry in the top navigation bar. The user can choose in the settings page whether 中 items also notify.

Add one new condition type to the existing alert rule engine: a 高 importance item for a given symbol within the last N minutes. It can be combined with price conditions using AND. A rule whose conditions are only news conditions must not be allowed to carry an order action; the UI must block that combination and explain why in Chinese.

Add a status line for every news source on the existing status panel: enabled or disabled, time of the last successful poll, and the last error if any.

## Step 5: testing and documentation

In mock mode, generate simulated news items and filings so the page can be tested when no network source is reachable. Write unit tests for the keyword scoring, the deduplication logic and the SEC rate limiter. Update the README section 使用说明 with how to enable each source, how to set the SEC contact email, and how to enable or disable Layer 2. List any difference between this prompt and the official documentation in the README.
