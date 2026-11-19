# moomoo 本地股票与期权监控看板

A local monitoring dashboard for US stocks / ETFs and US options, using the moomoo OpenAPI (OpenD) as the only data source. It runs only on this machine: the backend binds to `127.0.0.1`, and the browser never talks to OpenD directly. Automatic ordering exists behind a master switch that is **OFF by default**, and paper trading is the default environment.

## 使用说明

### 1. 安装 OpenD

本机已安装 moomoo OpenD 图形界面版 **10.12.7208**（比要求的 10.11.7108 更新），位置：

```
C:\Users\19288\AppData\Roaming\moomoo_OpenD\moomoo_OpenD.exe
```

启动方式：双击上面的文件，或按 Win + R 输入 `%APPDATA%\moomoo_OpenD\moomoo_OpenD.exe`。

如需重新安装或升级，请只从官方地址下载：<https://www.moomoo.com/download/OpenAPI>（或按官方 AI 安装指南 <https://openapi.moomoo.com/moomoo-api-doc/en/intro/ai.html> 操作），选择“可视化 OpenD”（GUI 版）。Python SDK 版本必须与 OpenD 版本一致（当前均为 10.12.7208）。

### 2. 登录并完成 API 问卷

1. 启动 OpenD，用 moomoo 账号（牛牛号、邮箱或手机号）手动登录。**本程序从不输入、保存或记录登录密码和交易密码。**
2. 首次登录时按提示完成 API 问卷和协议。
3. 在 OpenD 右侧设置中确认监听地址为 `127.0.0.1`、API 端口为 `11111`。
4. 如需使用状态栏中的“重新获取最高行情权限”按钮，请在 OpenD 设置中开启 Telnet（地址 `127.0.0.1`，端口 `22222`，与 `config.yaml` 中的 `opend.telnet_port` 一致）。当前 OpenD 尚未开启 Telnet，按钮会提示你开启。

### 3. 确认美股期权行情权限为 LV1

在 OpenD 的行情权限中确认“美股期权”为 **LV1**。账户总资产大于 0 或持有美股持仓时 LV1 免费；否则需要购买标注“适用于 OpenD”的 OPRA 期权行情卡。

本机已确认：美股 LV3、美股期权 LV1；股票订阅额度 300，期权订阅额度 60。

### 4. 安装依赖并启动后端

在 PowerShell 中执行（项目目录已建好虚拟环境 `.venv`）：

```powershell
cd E:\炒股
.venv\Scripts\python.exe -m pip install -r requirements.txt   # 已安装，可跳过
.venv\Scripts\python.exe scripts\check_opend.py               # 可选：连接测试（订阅前快照 US.QQQ）
.venv\Scripts\python.exe -m backend                           # 连接 OpenD 启动
.venv\Scripts\python.exe -m backend --mock                    # 模拟模式：不连接 OpenD，生成模拟行情
```

也可以直接双击 `启动看板.bat` 或 `启动模拟模式.bat`。

- 模拟模式默认使用单独的 `config.mock.yaml`（首次运行时从 `config.yaml` 复制），数据和日志写到 `data/mock`、`logs/mock`，不会影响实盘配置。
- 可选参数：`--config 路径`、`--port 端口`（默认 8600）。
- 按 Ctrl + C 停止：程序会退订已满 1 分钟的订阅并关闭两个连接；不足 1 分钟的订阅由 OpenD 在连接关闭约 1 分钟后自动释放（已实测）。

### 5. 在浏览器中打开看板

打开 <http://127.0.0.1:8600>。首次使用请点击状态栏中的“开启桌面通知”，提醒触发时才会弹出桌面通知（同时会高亮相关行并播放提示音）。

### 6. 功能速览

| 页面 | 功能 |
|---|---|
| 状态栏（常驻） | OpenD 连接状态、美股 / 美股期权行情权限、股票与期权订阅额度（已用 / 剩余）、各限频接口当前 30 秒窗口内的调用次数、美东时间与本地时间、交易环境与自动下单状态；最高行情权限被抢占时显示提示和“重新获取最高行情权限”按钮 |
| 股票 | 输入任意美股代码（moomoo 格式，如 `US.QQQ`）加入自选；每个代码可勾选报价、逐笔、摆盘、1 分 K、5 分 K，勾选立即订阅、取消立即退订；不足 1 分钟的退订会显示剩余秒数并到期自动退订；显示最新价、涨跌额、涨跌幅、最高、最低、成交量、最近一根已完成的 5 分钟 K 线收盘价，以及盘前 / 盘后 / 夜盘价格和时段标签；点击行展开摆盘、逐笔和 K 线 |
| 期权 | 输入标的后加载到期日，可多选；每个到期日显示 T 型报价表（中间为行权价，左看涨、右看跌，含买价、卖价、中间价、最新、成交量、未平仓、隐含波动率与希腊值），高亮最接近标的价格的行权价，可设置上下各 N 档；每个到期日可选“快照轮询”（不占额度，默认 5 秒，最快 1 秒）或“订阅”（占用期权额度）；点击合约旁的 + 固定到“期权自选” |
| 市场 | 基准 ETF 行情条（默认 SPY、QQQ、TQQQ、DIA、SOXX，可编辑）；① 板块涨跌：涨幅、跌幅各前 5，点击展开成分股；② 资金流向：净流入、净流出各前 10；③ 板块领涨：领涨股涨幅最大的 5 个板块（只统计达到规模门槛的股票），附 24 小时内的催化新闻；④ 重要新闻：时间线（每条只标注 高 / 中 / 低）、未来 7 天财报、按标的 / 重要性 / 来源筛选。只显示美股相关资讯。各列表条数可在 `config.yaml` 的 `market.top_n_sectors` / `top_n_flows` / `top_n_leaders` 中修改。高重要性新闻到达时弹出桌面通知、播放提示音，并在“市场”标签上显示计数 |
| 个股分析（默认关闭） | 默认隐藏，接口返回 404；在 `config.yaml` 中设 `stock_analysis.enabled: true` 并重启看板后启用。输入代码或公司名（带候选列表），回车或点击“生成分析”：分步显示进度（获取行情、获取期权链、检索新闻、生成分析、校验交易方案），结果按结论、公司概览、基本面与财报质量、估值与预期差、新闻（按 5 类分组）、花边新闻、关联公司、交易方案、监控清单与来源分区显示，每个分区可折叠。交易方案由服务端代码校验并重算盈亏，卡片底部按“可承受亏损 ÷ 每张最大亏损”计算张数。同一代码 30 分钟内直接使用缓存。详见第 11 节 |
| 市场热力图 | TradingView 官方 Stock Heatmap 嵌入组件：标普 500 成分股，按行业分组，面积为市值，颜色为涨跌幅；右上角可在 TradingView 中打开 |
| 设置（导航栏齿轮） | 资讯来源开关、SEC 联系邮箱、标的英文名、关注关键词、高 / 中重要性关键词、智能分类（Claude）开关与今日调用次数、中重要性是否通知 |
| 提醒 | 新建规则：最新价高于 / 低于、5 分钟 K 线收盘高于 / 低于、价格区间、期权中间价高于 / 低于、买卖价差大于、N 分钟内出现高重要性新闻；多个条件为“且”（AND）关系；可限定美东时段；仅触发一次或冷却后再次触发；规则保存在配置文件中，重启后自动加载。只包含新闻条件的规则不能关联下单 |
| 自动交易 | 总开关（默认关闭）、交易环境（默认模拟交易，切换实盘需两步确认）、硬性限制（修改需确认）、今日统计、托管持仓、当日委托、订单日志、紧急停止 |

如果输入的是美股指数代码（如 `US..IXIC`、`US.VIX`、`US.NDX`），看板会提示改用对应的 ETF（QQQ、VIXY/UVXY、SPY、DIA、IWM 等），因为 API 不支持美股指数行情。

### 7. 市场页面：数据方法与限制

数据方法已在 2026-11-11 用 OpenD 10.12.7208 实测确认：

- **板块范围**：默认使用美股行业板块（145 个），页面上可切换为概念板块（118 个）。板块列表和成分股每天获取一次并缓存。`get_plate_stock` 每 30 秒最多 10 次，所以每天第一次加载行业板块成分股约需 7 分钟，期间进度显示在页面顶部；加载中断后会从断点继续。
- **板块涨跌**：`get_market_snapshot` 接受板块代码，直接返回板块点位、昨收和上涨 / 下跌家数，所以涨跌幅直接来自 moomoo。只有某个板块缺少快照数据时，才按成分股市值加权计算。
- **资金流向**：`get_capital_flow` 不支持板块代码（OpenD 返回“只支持正股、窝轮和基金”）。因此**只统计各板块成交额前 5 的成分股**（规模达标的美股），板块净流入为这 5 只的当日净流入之和。每轮最多查询 60 只（本页限 24 次 / 30 秒，一轮约 1–2 分钟），从没有当日数据、最久未更新的股票查起；启动或切换板块类型后连续补查，直到所有板块的前 5 只都有当日数据，之后每 5 分钟一轮。页面显示每个板块“前 5 只占板块成交额”的比例。前 5 只的数量和每轮上限可在 `config.yaml` 的 `market.capital_flow_top_per_sector`、`market.capital_flow_calls_per_cycle` 中修改。也可在 `config.yaml` 中把 `market.capital_flow_method` 改为 `etf_proxy`，改用板块 ETF（XLK、XLF、XLE、SMH 等）作为代理。资金流只统计常规交易时段，不含盘前盘后。
- **板块领涨**：从成分股快照中选出每个板块的涨幅第一，只统计市值 ≥ 20 亿美元、且今日成交额 ≥ 2000 万美元的股票，两个门槛都可以在页面上修改。
- **刷新节奏**：常规交易时段每 60 秒刷新板块涨跌、领涨和基准，每 5 分钟刷新资金流；非交易时段保留上一交易时段的数据，并显示数据时间。
- **额度**：本页只用快照，不占订阅额度；通过同一个限频器以最低优先级运行，最多使用快照额度的一半（30 / 30 秒），用量显示在状态栏的“市场页快照”中，不会拖慢股票页、期权页和下单。
- **只看美股（不含中概股）**：`market.us_only: true`（默认）时，板块涨跌、资金流向和板块领涨都剔除中概股。
  - **中概股名单**来自三处：moomoo 的中概股板块成分股（如“热门中概股”“China's Terrific”“内地教育股”）；SEC EDGAR 中营业地址在中国大陆、香港或澳门的公司（按 20-F / 10-K 年报查询，每 7 天更新一次，需要已填写 SEC 联系邮箱）；`config.yaml` 中 `market.exclude_codes` 手动添加的代码。
  - **台湾公司不算中概股**：moomoo 的“热门中概股”里有 TSM、UMC、ASX，看板按 SEC 营业地址识别出台湾公司并保留。如果你也想剔除，把它们写进 `market.exclude_codes`；反过来，某只股票不想被当作中概股，写进 `market.keep_codes`。
  - **隐藏的板块**：名称是中国主题的板块（如“阿里概念”“腾讯概念”“DeepSeek概念股”“双十一”），以及中概股占一半以上或美股不足 3 只的板块（如“在线教育”）。页面说明行列出被隐藏的板块。
  - **重算的板块**：其余含中概股的板块（如“互联网零售”含 BABA、JD），涨跌幅改为剔除中概股后、按美股成分股市值加权重算；上涨 / 下跌家数、领涨股、成分股列表和资金流也只统计美股。
- **无法报价的代码**：OpenD 不提供美股 OTC 行情（如 AAGC），而一次快照请求中只要有一个这样的代码，整批都会失败。看板会自动找出这些代码并跳过，记录在 `data\live\cache\market\unsupported_codes.json` 中。
- **资金流覆盖率**：某板块已查询的前 5 只成交额不到其全部规模达标成分股成交额的 20% 时，暂不列入资金流排行，避免数字失真（`market.min_flow_coverage_pct`）；页面说明行显示因此未列入的板块数。切换板块类型后立即开始新一轮资金流查询；每次启动看板都会重新计算一次板块数据。

### 8. 资讯来源与设置

点击导航栏右侧的齿轮打开“设置”。

| 来源 | 内容 | 默认 | 说明 |
|---|---|---|---|
| moomoo 资讯搜索 | moomoo 官方 News Search 技能使用的公开接口 `ai-news-search.moomoo.com`，资讯与公告 | 开 | 不需要 OpenD。官方没有公布限频，所以按每个标的每 2 分钟 1 次、均匀分布轮询。BE 这类短代码会搜到大量无关新闻，请在设置中填写“标的英文名”（已预填 `US.BE = Bloom Energy`），否则只保留标题中真正出现该代码的新闻 |
| moomoo API 资讯 | OpenD `get_search_news`，资讯与公告，带来源名称和相关股票 | 开 | 每 30 秒最多 10 次；发布时间只到日期 |
| SEC EDGAR | 8-K（记录 Item 编号）、6-K、13D、13G；Form 4 默认关闭 | 开，但需要邮箱 | 首次使用请在设置中填写联系邮箱：SEC 规定自动访问必须在 User-Agent 中注明名称和联系邮箱，邮箱只保存在本机 `config.yaml`。每 60 秒轮询，最多每秒 5 次（SEC 上限每秒 10 次） |
| 财报日历 | OpenD `get_earnings_calendar`，未来 7 天自选标的财报，标注盘前 / 盘后 / 盘中 | 开 | 每 30 分钟刷新 |

**重要性打分**：

- **第一层，关键词规则**：始终开启，关键词可在设置中编辑，中英文均可。
- **第二层，智能分类（Claude）**：默认关闭。开启方法：
  1. 在启动看板的同一个终端设置环境变量，例如 PowerShell 中 `$env:ANTHROPIC_API_KEY = "你的密钥"`，或在 Windows 系统环境变量中设置，然后启动后端。密钥只从环境变量读取，不会写入任何文件。
  2. 在设置中打开“启用智能分类”。
  3. 只会发送标题、来源、时间和摘要前 500 字。
  4. 每日上限 300 次（`news.layer2.daily_cap`），用量显示在设置中；达到上限后自动回退到关键词规则。
  5. 模型名称在 `news.layer2.model`，默认 `claude-opus-5-5`；改为 `claude-haiku-5-5` 费用更低。
  6. 关闭方法：在设置中关掉开关即可。

**只看美股**：标题中带有非美股上市代码的条目（如 `(SZSE:300783)`、`600519.SH`、`0700.HK`）会被丢弃；moomoo API 资讯中相关股票全部是非美股的条目也会被丢弃。

**去重**：同一标的、发布时间相差 30 分钟以内、且标题相似（或指向同一篇文章 / 同一份 SEC 文件）的条目会合并为一个事件。资讯保存在本地 SQLite 数据库 `data\live\news.db` 中，30 天后自动删除。

### 9. 验收顺序（未全部通过前请勿切换到实盘）

1. **模拟模式**：用 `--mock` 启动，逐项确认界面功能。模拟模式状态栏下方有“模拟：行情权限被抢占 / OpenD 断开 / 设置标的价格”按钮，用于测试权限提示、断线重连和提醒、止损。
2. **连接 OpenD、美股盘中**（美东 09:30–16:00）：确认股票推送更新、期权链 T 型表、两种期权报价模式和额度显示。
3. **模拟交易端到端**：打开总开关（模拟交易），把一个期权合约固定到期权自选，新建一条必然触发的测试规则（例如“最新价 ≥ 1”）并关联该合约买入 1 张，观察：触发 → 按中间价下限价单 → 成交 → 止损 / 止盈 / 时间止损平仓 → 按“紧急停止”。最后检查订单日志 `logs\live\orders\orders-日期.jsonl` 是否完整。
4. 以上三步都通过后，才考虑切换到实盘交易；实盘需要你在 OpenD 界面手动解锁交易。

### 10. 文件位置

| 内容 | 位置 |
|---|---|
| 配置（OpenD 地址、限频与额度、自选、期权设置、提醒规则、硬性限制、市场页、资讯来源） | `config.yaml` |
| 资讯数据库（保留 30 天） | `data\live\news.db` |
| 板块列表与成分股每日缓存、中概股名单（`china_sec.json`）、无法报价的代码（`unsupported_codes.json`） | `data\live\cache\market\` |
| 运行日志 | `logs\runtime.log`（模拟模式 `logs\runtime-mock.log`） |
| 订单日志（每笔下单、改单、撤单、成交回报，含时间、合约、方向、数量、价格、触发规则与 API 原始返回） | `logs\live\orders\orders-YYYY-MM-DD.jsonl` |
| 期权链每日缓存 | `data\live\cache\` |
| 个股分析缓存（每个代码一个文件，30 分钟内有效）与当日用量 | `data\live\analysis\` |
| API 密钥（只由后端读取，模板见 `.env.example`） | `.env` |
| 提醒触发状态、自动交易当日状态 | `data\live\alert_state.json`、`data\live\trading_state.json` |
| 单元测试 | `.venv\Scripts\python.exe -m pytest` |

### 11. 个股分析与市场热力图

**开关（默认关闭）**：`config.yaml` 中 `stock_analysis.enabled: false` 时，「个股分析」tab 不显示，页面不发出任何分析请求，`/api/stock-analysis`、`/job/…`、`/search` 均返回 404，`/settings` 只返回 `{"enabled": false}`。改为 `true` 并重启看板即可恢复，下面的说明适用于启用后。市场热力图不受此开关影响。

**配置密钥**：把项目目录下的 `.env.example` 复制为 `.env`，填写 `ANTHROPIC_API_KEY=你的密钥`，然后重启看板。`.env` 只由后端在启动时读取（已在系统环境变量中设置的同名变量优先），密钥不会写入任何其他文件，也不会出现在前端代码、接口返回或浏览器请求里。模拟模式（`--mock`）不需要密钥，也从不调用 API：分析内容为明确标注的模拟内容，用于测试界面。

**数据流程**（服务端接口 `POST /api/stock-analysis`，请求体 `{"ticker": "NVDA"}`）：

1. 行情与基本面：moomoo OpenD 快照（价格、市值、市盈率等），以及 F10 数据：所属行业、公司概况、利润表、资产负债表、现金流量表、主要指标、估值、分析师一致预期、历次财报前后的股价与隐含 / 历史波动率。某一项取不到时记为“未取得”，不影响其余步骤。
2. 期权链：只取距今天（美东）7 个自然日以内的到期日，行权价取现价上下 20% 以内、每侧最多 30 档；每个合约包含行权价、到期日、买价、卖价、中间价、隐含波动率、未平仓量和当日成交量，并计算平值跨式价格。全部是 OpenD 实时数据，不用模拟数据代替。
3. 新闻：moomoo 资讯与公告，加上模型调用中的网络搜索（`web_search_20260209`，每次分析最多 20 次）。
4. 生成分析：服务端调用 `claude-opus-5-5`（effort `high`，流式），system prompt 为 `dashboard-prompt.md` 末尾的“分析规则”，第 1、2 步的数据以 JSON 放在用户消息中。模型只返回 JSON；解析失败时在同一对话里重试一次，再失败返回错误。
5. 校验交易方案（代码执行，不依赖模型）：期权腿到期日超过 7 个自然日、合约不在第 2 步的期权链中、未平仓量低于 500 张或买卖价差超过中间价 10% 的方案会被删除，并在页面上列出原因；盈亏平衡点、每张最大收益与亏损、所需资金或保证金按各腿中间价重新计算（裸卖期权的保证金按 Reg T 公式估算）；结论为“观望”时不显示任何方案。

**其他规则**：

- 只看美股：中概股不能分析，也会从候选列表和关联公司中剔除，名单与市场页相同（`market.keep_codes` / `exclude_codes` 可调整）。
- 新闻和花边新闻的来源链接如果没有出现在本次网络搜索结果中，会标注“未在检索结果中”，请自行核实。
- 同一代码 30 分钟内重复查询直接返回缓存，页面显示“缓存结果”和生成时间；“重新生成”会跳过缓存。缓存保存在 `data\live\analysis\`，重启后仍有效。
- 每个美东自然日最多分析 50 次（`stock_analysis.daily_cap`）。一次分析通常需要 1 至 4 分钟，费用取决于搜索次数和输出长度，页面底部显示本次的 token 用量。
- 所有参数在 `config.yaml` 的 `stock_analysis` 中。

**市场热力图**：使用 TradingView 官方嵌入组件（不是 iframe 嵌入 tradingview.com 页面），切换到该 tab 时创建组件，离开时清空容器，所以来回切换不会出现两个热力图。`colorTheme` 跟随看板当前的深浅色（看板目前只有浅色，所以是 `light`；以后加入深色模式时会自动切换并重新加载组件）。需要能访问 `s3.tradingview.com`。

---

## Architecture

```
browser (frontend/, vanilla JS)  ──REST + WebSocket──▶  FastAPI backend (127.0.0.1 only)
                                                          │
                                       services: stocks · options · alerts · trading · status · subscriptions
                                                 · market (市场 page) · news (资讯 sources, scoring, dedup, SQLite)
                                                          │
                                       OpenDClient ──▶ SlidingWindowLimiter (every call) ──▶ Gateway
                                                                                              ├─ MoomooGateway: 1 OpenQuoteContext + 1 OpenSecTradeContext
                                                                                              └─ MockGateway (--mock, incl. simulated plates / flows / news)
                                       news HTTP sources ──▶ separate limiter (moomoo search safety cap, SEC 5 / s) ──▶ httpx
```

- `backend/market/` — `calc.py` (pure: weighted change, size filter, leader, sign-aware top / bottom) and `service.py` (daily, resumable plate + constituent cache; 60 s sector / leader / benchmark refresh and 5 min capital-flow cycle during regular hours; consumer `market` capped at half of the snapshot budget and at the lowest priority).
- `backend/news/` — `sources.py` (A moomoo news search, B OpenD `get_search_news`, C SEC EDGAR, mock), `scoring.py` (Layer 1 keywords + SEC form rules), `dedup.py` (event grouping), `store.py` (SQLite, 30-day retention), `classifier.py` (optional Layer 2 with the Anthropic SDK: structured JSON output, daily cap, key only from `ANTHROPIC_API_KEY`), `service.py` (polling schedules, notifications, settings, earnings calendar D).
- `backend/analysis/` (个股分析) — `data.py` (quote, F10 fundamentals, option chain <= 7 days with quotes, moomoo news), `llm.py` (one streamed Claude call with `web_search_20260209`, JSON only because web-search citations cannot be combined with structured outputs, `pause_turn` resume, one retry on unparsable JSON, refusal fallback), `validate.py` (pure: JSON extraction / normalization, the four trade-plan removal rules, expiry payoff -> break-evens, max profit / loss, capital or Reg T margin), `service.py` (30 min cache, one job per symbol, progress over WebSocket `analysis_progress`, daily cap, US-only filtering, code / name search), `mock.py` (labelled simulated analysis for `--mock`, no API call). `backend/envfile.py` loads `.env` into the server process only.
- Frontend: `frontend/analysis.js` (个股分析 tab) and `frontend/heatmap.js` (TradingView embed widget mounted on show, emptied on leave, `colorTheme` from the rendered page background).
- New alert condition `news_high_within` (a 高 item for the symbol within the last N minutes); rules made only of news conditions are rejected if they carry an order action, in the API and in the editor.

- `backend/ratelimit.py` — one sliding 30 s window per endpoint (or shared bucket). Requests over the limit are **queued, never sent early**. Waiters are served by priority: cancels > orders > UI actions > background polling. `reserve_high` keeps part of the snapshot budget free for order pricing, so orders still go out quickly while polling is busy. Quote and trade SDK calls also run in separate thread pools.
- `backend/alerts.py` — pure rule engine (AND conditions, ET time windows incl. across midnight, fire-once / cooldown, missing data never fires).
- `backend/services/subscriptions.py` — quota pre-check from `query_subscription` before every subscribe (rejected in Chinese, nothing sent), 60 s minimum hold with automatic scheduled unsubscribe, restore after reconnect, release on shutdown.
- `backend/services/options.py` — expirations and chains fetched once per US trading day and cached on disk; per-expiration quote mode; snapshot polling batches ≤ 400 codes.
- `backend/services/trading.py` — master switch, environment switch with challenge token + typed phrase, hard limits, managed positions (stop loss / target / time stop), emergency stop, JSONL order log.
- `backend/services/status.py` — connection, quote rights, quota, per-endpoint usage; lost highest quote right detected from `SysNotify` QOT_RIGHT pushes and from periodic `get_user_info` (rights lower than the best seen this session).
- Reliability: the SDK auto-reconnects; on every (re)connect the dashboard verifies OpenD's subscription list and re-subscribes anything missing (tested in mock with a simulated OpenD restart that wipes subscriptions).
- Security: Host-header check (DNS rebinding), state-changing requests need the `X-Dashboard` header and a local `Origin` (blocks cross-site requests), WebSocket origin check. `unlock_trade` is never called; no password is read, stored or passed anywhere.

## Documentation check (Task 2) and differences from the prompt

Checked against Moomoo API Doc v10.12, the SDK source of `moomoo-api` 10.12.7208, and the official `moomooapi` skill docs (`API_REFERENCE.md`, `API_LIMITS.md`, shipped in `opend-skills.zip` and installed under `.claude/skills/`). Rate-limit and quota values live in `config.yaml`.

| Topic | Prompt | Documentation / observed | What the code does |
|---|---|---|---|
| Snapshot limit | 60 / 30 s, 400 codes | Same (verified) | as documented; polling may use 54, 6 reserved for orders/UI |
| Option chain limit | 10 / 30 s, 30-day span | Same; expired chains cannot be queried | chains fetched once per day and cached; spans split into ≤ 30 days |
| Place order | 15 / 30 s, combo shares it | Same, **per acc_id**, and ≥ 0.02 s between calls | `min_interval_sec: 0.02`; combo uses the same bucket |
| Modify / cancel | not specified | `modify_order` 20 / 30 s per acc_id, ≥ 0.04 s between calls | cancellations use this bucket |
| Expiration dates | not specified | 60 / 30 s | configured |
| `cancel_all_order` | used for emergency stop | **Not supported for paper trading accounts** | emergency stop also lists open orders and cancels each with `modify_order(CANCEL)`; in live trading it first calls `cancel_all_order(trdmarket=US)` |
| Combo orders | verify in paper first | Live answer from OpenD for this account: **“模拟交易不支持组合期权”** (paper trading does not support combo options) | combo option is disabled in the UI once detection fails; live combos additionally require a successful paper combo, so they stay disabled for this account |
| Option subscription | 1 quota per chain per data type | Same; option pool separate (20/60/200/400 tiers) | **QUOTE pushes carry no bid/ask**, so subscription mode uses QUOTE + ORDER_BOOK = 2 option quotas per chain (configurable) |
| Option quota fields | “read remaining quota from the official query” | The doc page lists only `total_used/own_used/remain`; SDK 10.12 also returns `option_used_quota/option_remain_quota` | both pools are read from `query_subscription` |
| Highest quote right | “official command or endpoint” | It is an **OpenD Telnet command** `request_highest_quote_right` (10 per 60 s), not an SDK function | sent over Telnet to `opend.telnet_port`; Telnet must be enabled in OpenD |
| Extended hours | show pre / after / overnight | Snapshot has `pre_*`, `after_*`, `overnight_*`; subscribe `session` allows RTH/ETH/ALL, **not OVERNIGHT** | candles/ticks subscribed with `session=ALL`; watchlist also refreshed by snapshot every 5 s |
| Paper trading sessions | — | Paper trading does not support US pre-market / after-hours / overnight orders | entries are blocked after the time stop; place paper tests in regular hours |
| 5 minute candle close | — | `time_key` is the bar **end** time in ET (verified live) | “5 分钟收盘” = latest candle with `time_key ≤ now (ET)` |
| Implied volatility | — | snapshot returns percent (e.g. 29.6 = 29.6 %) | displayed as % |
| SDK package | `pip install moomoo-api` | Same; installed 10.12.7208 to match OpenD 10.12.7208 | pinned in `requirements.txt` |
| OpenD version | 10.11.7108 or newer | Download page now serves 10.12.7208 | installed 10.12.7208 (upgraded from 9.6.5618) |
| AI setup guide | follow it | It installs skills into the global `~/.claude/skills` | installed into the project `.claude/skills/` instead, to keep the global setup untouched |

### Market page and news monitor (checked 2026-11-11 against doc v10.12, SDK 10.12.7208 and live OpenD)

| Topic | Prompt | Documentation / observed | What the code does |
|---|---|---|---|
| `get_plate_list` / `get_plate_stock` | confirm US plates and limits | US INDUSTRY 145 plates, CONCEPT 118, ALL 351, REGION 0, OTHER rejected; both 10 / 30 s | industry by default, concept switchable; constituents loaded once per day, spread by the limiter (~7 min for 145 plates), cached and resumable |
| Plate snapshot | use if it gives a change rate | `get_market_snapshot` accepts plate codes: plate level, previous close and `plate_raise/fall/equal_count` | sector change taken from the plate snapshot (labelled); weighted constituent average only as fallback |
| Capital flow | plate code if accepted | **Plate codes rejected** (“只支持正股、窝轮和基金”); 30 / 30 s; ETFs accepted; intraday `in_flow` is cumulative and covers regular hours only | sum of the net inflow of each sector's top 5 size-filtered constituents by dollar volume; ≤ 60 codes per round (1–2 min at 24 / 30 s), unmeasured / stalest first, rounds back to back until every top-5 stock has today's value, then every 5 min; values from an earlier trading day are ignored; coverage (top-5 share of the sector's eligible dollar volume) shown, sectors below `min_flow_coverage_pct` (20) not listed; `etf_proxy` mode available |
| Heat map | — | `get_heat_map_data` (60 / 30 s) also returns plate change and a leader | not used (the prompt's method is followed); limit recorded in config |
| Budget | at most half of the snapshot budget | — | consumer cap `market: 30` of 60 / 30 s, lowest priority; shown in the status panel |
| `Moomoo API Skills/.../API_REFERENCE.md` | read it in moomoo-agent-hub | The hub **removed** the `moomooapi` skill (moved to a CLI); the file only exists in the official `opend-skills.zip` | used the copy from `opend-skills.zip` (installed under `.claude/skills/`) |
| Source A | moomoo News Search skill | `GET https://ai-news-search.moomoo.com/news_search` (keyword, size ≤ 50, news_type 1/2/3, lang, sort_type); public, no key, **no rate limit and no endpoint terms documented**; titles contain `<em>` tags | enabled; 1 request per target per 2 minutes spread evenly, plus a 20 / min safety cap; company names used as keyword (short tickers such as BE return unrelated news; ticker results are filtered by headline mention) |
| Source B | news / announcement functions | `get_search_news(keyword, max_count ≤ 100, NEWS/NOTICE/RATING)`, 10 / 30 s; **`publish_time` is a date only** | used every 5 min per symbol; merged with source A by moomoo post id; time shown as date |
| Source C | SEC submissions API, ≤ 5 / s, forms 8-K / 6-K / SC 13D / SC 13G | Fair access: name + contact email User-Agent, max 10 / s; `acceptanceDateTime` is UTC; **since Dec 2024 the forms are named `SCHEDULE 13D` / `SCHEDULE 13G`** | both old and new form names watched; email asked in the settings sheet on first setup and stored in `config.yaml`; disabled until set |
| Source D | earnings date function | `get_earnings_calendar` exists, 60 / 30 s, **range ≤ 7 days**, `pub_type` BEFORE / AFTER / REGULAR | next 7 days of the whole market fetched every 30 min, filtered to the watchlist |
| Layer 2 | Anthropic API, current model name in config | Current model IDs per Anthropic docs (2026-11): `claude-opus-5-5` (default), `claude-haiku-5-5` (low cost); structured outputs via `output_config.format` | off by default; JSON schema output; server-side refusal fallback on (`fallbacks: default`); 300 calls / day cap |
| News body | “mentions only in the body” | Sources A and B return headlines without body text | keywords found only in a summary count one level lower; ranking uses headline mentions |
| US stocks only (user request) | sectors, flows and leaders must not include China stocks | moomoo has no country field; its China plates (热门中概股, China's Terrific, 内地教育股) cover ~46 codes and include Taiwan's TSM / UMC / ASX. EDGAR full-text search (`efts.sec.gov/LATEST/search-index`) filters by business location code (F4 mainland China, K3 Hong Kong, F5 Taiwan), **but filings from 2026 often carry only the city** ("Beijing"), and single pages sometimes answer HTTP 500 | China set = moomoo China plates + SEC registrants based in mainland China / Hong Kong / Macau (coded 20-F / 10-K of the last 36 months, plus recent 20-F matched by place name; ~30 requests per week through the shared SEC limiter, retried with backoff, partial results merged with the previous list) + `exclude_codes`; Taiwan-based companies and `keep_codes` stay in. China-themed and mostly-China plates are hidden; other plates with China stocks are recomputed from US constituents (method `us_only`) |
| Unquotable codes | — | one OTC code ("暂不提供美股 OTC 市场行情 AAGC") fails the whole snapshot batch | the named code (or, if none is named, found by bisection) is dropped, remembered in `unsupported_codes.json` and skipped everywhere |

## Design (Apple Liquid Glass skill)

The UI follows `design/apple_liquid_glass/` (MIT, LICENSE kept): `tokens.css` is served unchanged as `/static/tokens.css` and every color, radius, shadow and spacing value references it; unified white panels with hairline dividers for every table; glass only on the sticky nav (scroll-edge hairline), toasts and modal sheets; sheets materialize (blur + scale + opacity) with `--ease-spring`, same-path exit, origin anchored to the trigger, and all three `prefers-reduced-*` / `prefers-contrast` queries; system font stack incl. PingFang; Lucide line icons from `icons.md` only where they name an action.

Project-specific overrides (as requested):

1. **Price direction colors** are an explicit exception to the grayscale rule: green = up, red = down, only on change, % change and P&L. Everything else is grayscale plus the single blue accent (the emergency stop is a black pill, form errors are dark text, not red).
2. **Wider container**: the monitor views use `max-width: 1440px` instead of 1080 px; tables still sit on the unified white panel.
3. **All numbers use `tabular-nums`.**
4. Consciously waived from `checklist.md`: dense desktop tables use 26–36 px controls instead of 44 px touch targets; long-body line-height 1.85 does not apply (no long prose).
5. **Option chain colors**: call columns have a green tint and put columns a red tint, stronger when in the money (call: strike < underlying; put: strike > underlying). The largest 未平仓 on each side among the displayed rows is yellow, bold and labelled 最大, with ties all marked. The strike column stays neutral, and the ATM row overlay and rules are drawn above the tints. A one-line legend sits above each chain. All of these colors are the `--chain-*` variables at the top of `frontend/app.css`. They are light theme only, because the dashboard has no dark theme.

## Acceptance status

| Step | Status |
|---|---|
| 1. Mock mode, every UI feature | Passed: `tests/test_api_mock.py` (watchlist, index rejection, 1-minute rule, quota, chain + both modes, pinning, alerts, master switch, two-step live confirmation, quote-right loss and re-request, disconnect + subscription restore, paper rule end to end with trigger → mid limit order → fill → stop → emergency stop → complete log) and a headless Edge click-through of every page without JS errors. |
| 2. OpenD during market hours | Partly verified on 2026-11-10 during the **overnight** session: connection, LV3 / LV1 rights, quota display, QQQ push updates (overnight price), option chain (194 strikes), snapshot mode with bid/ask/IV/Greeks, subscription mode (2 option quotas, order-book pushes), scheduled unsubscribe, combo detection, quota released on shutdown. **Re-check during regular hours.** |
| 个股分析 / 市场热力图 (2026-11-15) | Mock mode: headless Edge click-through of both tabs without JS errors (candidates, 5 progress steps, 9 sections, sizing calculator, cache, regenerate; one heatmap after switching tabs back and forth; 390 px without horizontal scroll). LLM path checked against a local fake Messages endpoint (request shape, `pause_turn` resume, one retry). **Not yet run with live OpenD F10 data and a real `ANTHROPIC_API_KEY`**: OpenD was not running and no key was configured. |
| 个股分析开关 (2026-11-15) | 个股分析 is switched off by default (`stock_analysis.enabled: false`). Live OpenD probe for NVDA before switching off: quote, statements, valuation, consensus, earnings moves, 3 expiries within 7 days with quotes and moomoo news all returned; never run with a real Anthropic key. Headless Edge: tab hidden, a saved `tab=analysis` falls back to 股票, no `/api/stock-analysis` requests, heatmap and other tabs unchanged. Verified. |
| 3. Paper trading end to end | Not yet run against OpenD (needs regular market hours; options do not trade overnight). Run it as described in 使用说明 step 9. |

Unit tests: `tests/test_ratelimit.py` (sliding window, margin, min interval, reserve, priority, shared bucket, cancellation, queueing) and `tests/test_alerts.py` (parsing errors in Chinese, every condition type, AND, ET windows, fire-once / re-arm, cooldown, missing data, multi-symbol rules, persistence, US session clock). `tests/test_analysis.py` covers the 个股分析 checks (payoff math for long / spread / cash-secured put / straddle / iron condor / naked call, each removal rule, 观望, recomputed values replacing the model's, JSON split by citations, `.env` loader) and the mock API flow (progress, cache, regenerate, search, US-only rejection, live branch with a stand-in model). Run `.venv\Scripts\python.exe -m pytest` — 139 tests.
