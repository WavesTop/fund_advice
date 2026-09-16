# fund_advice

面向 A 股相关基金和板块的投资建议与决策复盘工具，围绕三个同等重要的产品目标：

- **发现投资机会**：提供较全面的基金库供浏览和搜索，展示基金 K 线图或净值走势、公开披露的股票和集中板块，以及短期、中期和长期的板块机会。
- **辅助投资决策**：结合用户持仓与要求，同时提供三个周期的基金推荐和操作建议。
- **持续复盘与策略改良**：保留系统当时的建议和用户自己的决定，持续对比用户实际结果与系统建议的模拟结果。系统根据复盘识别策略可能存在的问题，提出改良方向，并通过新旧方案对比和后续跟踪检验改良效果。

少数未收录的基金可由用户提供代码，交由系统补充。

## 当前状态

已实现 §3.1 的前端界面与演示交互，并开始接入 §3.2 的真实基金数据。基金库现从本地 FastAPI 与 SQLite 查询真实基金目录；已采集的基金详情展示真实净值或场内日 K 线。“基金与板块 → 板块机会”已接入三周期轻量证据筛查：真实指数行情、经过核验的行业统计与有限估值观察，展示支持、反证和待确认条件。持仓、旧建议交互、收益复盘、策略及原板块详情仍为演示；默认建议入口改为当前真实研究，正式投资推荐尚未完成。

**当前不可用于真实投资或记账。** 基金目录以及详情页明确标注来源的净值／K 线来自本地数据库；未采集序列保持空白。明确标注演示的建议、持仓和收益仍为虚构样例，填写的演示记录仅保存在当前浏览器的本地存储。精确核算、模拟、AI 调用和后台执行仍待后续阶段接入。

### 当前优先改进：展示与研究

本批修改集中于基金／板块展示、三周期真实研究和对应基金关联。基金查询恢复搜索词与页码，详情保留真实身份并区分行情／入库时间；板块默认三周期摘要，可切完整证据。`/advice` 默认查看当前真实研究，交互演示通过 `/advice?mode=demo` 进入，旧演示决策记录保持可读。

基金关联仅使用已存储的明确指数关系，展示来源和核验时间；名称相似、行情领先或关联存在均不等于投资推荐。当前没有通过完整基金择优、历史验证和个人约束，不生成正式买卖建议或金额。完整输入快照、行业投资候选和对应基金筛选的后续步骤，以及本批尚未完成的前端／真实数据验收，见 [投资分析计划 §1.4](docs/investment-analysis-plan.md#14-轻量入口)。

### 基金库与板块机会工作台

卡片分页、来源精确的真实板块详情、浏览记录与清空搜索、采集后重新评估和证据契约已实现。后端回归通过；本轮真实 HTTP 连续 5 次调用均因执行环境 DNS 失败返回 502，不能视为来源可用；锁定依赖下的前端构建／浏览器和成功的真实源联调仍待验收。下一步应复查现有实现，不重复开发同一入口。规则、数据缺口与测试范围分别维护在界面设计 §4.1.1／§4.3.1、数据设计 §1.4、投资分析设计 §1.5／§1.8；唯一状态表为投资分析计划 MW1。

### 市场数据一致性与真实图表

基金详情新增已存储价格／净值切换与共同日期区间，关联指数同步查看；刷新返回成功／部分成功／失败，分别保留实际日期和原因。板块完整证据可按来源精确读取真实日线；跨源同名仅作参考，不表示成分或权重相同。

启动自动应用新增关系核验迁移；旧证据保留，旧关系歧义或最近核验失败时不会默认选一个指数。首次更新前备份本机数据库。当前仍未完成业务有效区间、完整交易日历、持仓穿透、总收益和正式推荐。已实现范围与实际验证统一见 [投资分析计划 MC1](docs/investment-analysis-plan.md#15-市场一致性与真实图表补齐mc1避免重复实施)，避免重复实现。

## 完整启动流程

需要安装 Git、[uv](https://docs.astral.sh/uv/) 和 Node.js 24 LTS。首次安装依赖需要联网；之后日常启动可以使用锁定文件和本地缓存。

### 1. 安装依赖

```bash
git clone https://github.com/WavesTop/fund_advice.git
cd fund_advice
uv sync --locked
cd frontend
npm ci
cd ..
```

### 2. 初始化本地数据

首次启动可以先导入真实基金目录，并采集全部可核验行业板块。两条命令都会访问公开数据来源：

```bash
uv run --locked --offline python scripts/import_fund_catalog.py
uv run --locked --offline python scripts/import_sector_heat.py --industry-only
```

行业命令完整读取东方财富行业层级目录，只纳入能与同花顺标准行业严格核对并取得日线的行业。重复运行会刷新本地快照；单个来源失败会保留可核对的旧数据和失败原因。

### 3. 同时启动网页与数据服务

在仓库根目录运行：

```bash
uv run --locked --offline python scripts/start_local.py
```

脚本会启动 FastAPI 数据服务 `http://127.0.0.1:8000` 和 Vite 网页 `http://127.0.0.1:5173`。浏览器打开[本地界面](http://127.0.0.1:5173)，后端状态可通过[健康检查](http://127.0.0.1:8000/health)确认。两项服务只监听本机地址；停止时在启动终端按 `Ctrl+C`。

如果提示端口已占用，先关闭之前运行本项目的终端进程，再重新执行启动命令。不要同时运行 `scripts/start_local.py` 和单独的 `npm run dev`／`uvicorn`。

### 4. 使用与刷新

基金库未提交搜索时，在搜索框下显示最近浏览的真实基金卡片；只有基金详情成功读取且身份匹配才记录，搜索本身不算浏览。点击搜索右侧“清空”会清除输入、已提交搜索和页码，返回浏览记录第一页，不删除浏览记录。浏览代码按最近访问顺序在当前浏览器保存，去重并最多保留 60 个；卡片身份及关系重新查询本地数据库，旧搜索记录不能反推从未记录的浏览历史。浏览／搜索卡片均按容器宽度每页 6 或 8 张，支持上一页／下一页、最近搜索复用、返回时恢复搜索词和页码。卡片只为唯一且通过当前核验的指数关系提供“查看板块／指数详情”链接；名称相似、旧关系或缺少证据不生成链接。真实板块详情按代码、来源和研究范围读取，不回退到原演示详情。尚未采集的净值和持仓保持缺项。

网页保留三个明确操作，行业采集与计算合并为一个“重新评估”：

| 入口 | 实际行为 | 不包含的内容 |
| --- | --- | --- |
| 基金库“更新基金目录” | 联网获取并导入基金份额身份，完成后重读当前搜索结果。 | 不批量下载全库净值／K 线。 |
| 基金详情“获取真实数据／更新数据” | 保留已有真实采集链路，更新该基金序列并核验、更新关联指数；分别返回成功或失败。 | 不保证所有场内基金自动同时取得价格和净值。 |
| 板块机会／真实行业详情“重新评估” | `POST /api/sectors/refresh` 联网采集可核验行业目录、日线及成分，成功或部分成功后在同一次请求内计算并返回评估；前端直接显示该响应，不另发成功后的 GET。 | 不更新经营／估值人工快照，也不批量更新已有参考指数；参考指数仍沿关联基金详情更新。不调用 AI、不执行交易。 |

联网更新显示进行中、成功、部分成功或失败；失败项保留可核对的旧资料，失败后也重读本地已提交状态，不把原数据当作本次成功结果。已经展开的板块日线在资料版本变化后重新读取，不继续展示旧图缓存。覆盖面板分别显示行业池／参考指数池的行情可比数、有效经营依据数和估值日期覆盖，不等于投资评分。

若“重新评估”返回 HTTP 404，先核对本地后端与网页版本及代理目标；当前源码已声明该 POST 路由，404 不应直接归因于行情网站。开发启动会检查 `/health` 的 `api_contract=market-workbench-v2`，拒绝静默复用不兼容的旧后端；请停止旧项目服务后重新运行 `scripts/start_local.py`，不自动终止其他进程。此诊断不表示已经定位用户本机 404 的唯一原因。

两个批量网页入口适用于当前本机单进程服务，同一数据库／目标重复提交返回 409；目录采集最多 90 秒，行业最多 600 秒，超时返回 504。关闭页面只停止浏览器等待，不承诺撤销已提交数据；进程重启后的持久任务查询和断点恢复尚未实现。更新前仍应备份本地数据库。

也可在另一个终端进入仓库根目录，继续使用原行业采集命令：

```bash
uv run --locked --offline python scripts/import_sector_heat.py --industry-only
```

命令完成后重新打开板块机会页面即可读取本地计算；点击“重新评估”会再次联网采集，不再是单纯读取。默认数据库位于 `runtime/database/app.sqlite3`；该目录是本机运行数据，不纳入版本控制。

可在“设置 → 界面验收场景”切换空白、加载、断网、保存失败、资料过期与 AI 意见冲突。请勿填入真实个人资料或密钥；AI 配置只预览发送范围，不会访问外部服务。

板块机会当前以全部可核验行业为主池：完整读取东方财富行业层级目录，再以分类和严格同名关系核对同花顺标准行业日线。概念、风格、地区和资格集合不进入行业主池。页面支持名称／代码搜索，以及成交额、短期、中期、长期走势强度的正反向排序和分页。成交额是活跃度，不是投资价值；走势强度只在同一价格截止日且完整观测日期序列相同的样本间排名，不是未来上涨概率；完整交易日历尚未接入，上海时间 16:00 前排除当日日线是保守截止规则，不等于已经验证最新应有交易日。资料不足、采集失败、日期不齐的行业不参与当次排名。

页面顶部的三周期优势板块先要求相对强度进入可比较行业前 25%，再核对区间方向、均线位置和最大回撤，区分“趋势领先”“回撤中修复”“领先后回落”和“高波动领先”。短期、中期、长期分别表示未来约 1 周至 1 个月、1 至 3 个月、3 至 6 个月的观察范围；20、60、120 个交易日只是对应的历史走势佐证窗口。板块身份按来源代码、分类体系和业务日期留存版本，成分股按榜单日期保存快照；来源未提供官方权重，因此页面不以总市值替代权重。原有三个真实指数及其行业背景保留在“已有指数观察”，不向其他行业套用未经核实的基金、经营或估值关系。

命令行刷新全部可核验行业及日线使用（与网页入口复用同一采集实现）：

```bash
uv run --locked --offline python scripts/import_sector_heat.py --industry-only
```

该命令会访问公开行情来源，保存真实覆盖与失败原因；榜单失败时保留旧榜并显示失败标识。行业快照与估值观察保存在 `config/sector-industry-evidence.json`、`config/sector-valuation-evidence.json`，含发布日期、核验时间和来源限制；它们不会自动更新。新增板块的经营或估值依据未补齐时，页面明确标记“暂不能判断投资方向”。方法与边界见[开发方案 §1.5](docs/investment-analysis-design.md#15-轻量板块机会的当前实现)。

## 检查与测试

在 `frontend` 目录运行：

```bash
npm run format:check
npm run build
npm test
npm run test:e2e
```

构建包含 TypeScript 严格检查；单元／组件测试使用 Vitest，完整浏览器流程使用 Playwright 和 Axe。浏览器测试默认使用本机 Google Chrome，且会按需启动本地预览。运行环境需允许启动浏览器和监听本机端口。HTML 测试报告在 `frontend/playwright-report/`，失败轨迹与截图在 `frontend/test-results/`，均不纳入版本控制。生产输出在 `frontend/dist/`，可用 `npm run preview` 本机预览。

当前工作台修改的实际检查及待验收范围见[界面测试报告](docs/ui-test-report.md#当前工作台验证mw1)，实施状态统一见[投资分析计划 MW1](docs/investment-analysis-plan.md#16-基金与板块工作台补齐mw1)。历史报告通过不代表本轮前端或真实源联调已经通过。

### 行业更新的连续真实接口验收

后端回归使用项目已有 unittest：`uv run --locked --offline python -m unittest discover -s tests -v`。ASGI 契约测试使用仓库内标准库测试客户端，不再隐式依赖锁文件未声明的 httpx；它不是浏览器／代理或真实来源测试。

启动服务后，可用以下命令连续调用真实更新接口并输出逐次 JSON 结果。**此命令会刷新目标服务实际配置的数据库**；先备份，验收应启动指向隔离数据库的服务，不与 CLI 或其他进程并行刷新。默认 5 次、间隔 2 秒，任一失败、部分成功或缺少评估均非连续通过。

```bash
uv run --locked --offline python -m scripts.verify_market_refresh --attempts 5 --base-url http://127.0.0.1:8000
```

代理链路复查时把地址改为实际 Vite 本机地址。成功响应还须人工核对真实行情日期、行业目录／纳入数、日线及成分成功数；5 次成功只证明这段时间的链路表现，不证明长期可用、最新交易日齐全或策略有超额收益。本轮实际结果、环境及未完成检查见上述界面测试报告；不得把测试替身的 200 与真实网络成功混为一谈。

## 基金来源探针

在仓库根目录使用 `uv`；Python 与 AKShare 版本由 `.python-version`、`pyproject.toml` 和 `uv.lock` 固定。首次同步需要联网：

```bash
uv sync --locked
uv run --locked --offline python scripts/probe_fund_source.py --list-samples
uv run --locked --offline python -m unittest discover -s tests -p 'test_probe_fund*.py' -v
uv run --locked --offline python -m unittest discover -s tests -p 'test_*quote*.py' -v
```

下面的探针命令会访问公开来源，运行结果保存在忽略提交的 `runtime/probes/`；`uv --offline` 只阻止依赖下载，不阻止探针联网。

```bash
uv run --locked --offline python scripts/probe_fund_source.py --catalog
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset exchange-nav --code 510050 --start-date 20251031 --end-date 20260115 --timeout 40
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset etf-daily --code 513660 --start-date 20260415 --end-date 20260424
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset lof-daily --code 160706 --start-date 20000101 --end-date 20260913
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset etf-daily --code 510050 --provider tencent --start-date 20250101 --end-date 20260913 --timeout 45
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset etf-daily --code 510050 --provider baostock --start-date 20260901 --end-date 20260913 --timeout 45
uv run --locked --offline python scripts/probe_fund_timeseries.py --dataset calendar --start-date 20260213 --end-date 20260228
uv run --locked --offline python scripts/probe_fund_disclosure.py --dataset holdings --code 005911 --year 2025 --timeout 40
uv run --locked --offline python scripts/probe_fund_disclosure.py --dataset fee --code 009314 --indicator '赎回费率' --timeout 40
uv run --locked --offline python scripts/probe_fund_disclosure.py --dataset dividend-events --code 510050 --year 2025 --timeout 60
uv run --locked --offline python scripts/probe_fund_disclosure.py --dataset split-events --code 515050 --year 2026 --timeout 60
```

序列探针支持 `unit-nav`、`cumulative-nav`、`exchange-nav`、`etf-daily`、`lof-daily`、`calendar`。除日历外必须传六位基金代码；日期格式为 `YYYYMMDD`。每次只请求一个数据集，运行后输出包含 `report.json` 的目录。退出码 `0` 为取得候选数据，`3` 为空结果，`2` 为参数、来源、数据校验或输出失败。

行情默认 `--provider auto`。ETF 依次尝试新浪、腾讯、BaoStock、东方财富，LOF 依次尝试新浪、腾讯、东方财富；网络、超时、上游调用失败、空结果或无法证明历史完整时继续下一来源。可指定 `sina`、`tencent`、`baostock` 或 `eastmoney` 单独核验，实际来源和尝试结果写入 `provider`、`attempts`。每个来源分别受 `--timeout` 硬超时限制，同一来源不重试。身份错配、无效行情和字段校验失败会立即停止，防止其他来源掩盖脏数据。净值和日历继续使用原接口。

新浪一次返回可得历史，成交量实际为份、成交额为元；腾讯按年请求，原始成交量为手、成交额为万元，使用十进制换算。腾讯端点最多返回 640 条且可能忽略开始日期，因此按窗口本地筛选并检查截断，逐年范围与返回条数见 `source_evidence`。腾讯数值可能经过舍入，两源结果不拼接、不求平均。行情数据为经过样本核对的不复权日线；场外基金使用净值序列。

BaoStock 免费登录接口已验证沪深 ETF，返回成交量为份、成交额为元；当前实测只返回有限的近期历史，探针会把缺失长历史识别为 `truncated_response` 并继续尝试东方财富。BaoStock 未覆盖已测 LOF，因此不进入 LOF 自动链。同花顺接口存在间歇性 502，且 LOF 日期集合与新浪有明显差异；网易公开历史接口持续返回 502；Tushare 需要用户 Token。这些来源不加入无需配置的默认链。

新浪先用 ETF／LOF 分类目录核对代码，腾讯核对返回的证券代码与类型；身份与请求不符或没有身份依据时明确报错。历史退市基金可能需要另外补充身份依据，不能由代码前缀直接认定类型。

复查时对照 `adapter` 与 `normalized_rows`，查看 `source` 和 `selected` 的条数及日期范围、`normalization` 的字段／单位和 `limitations`。普通净值与日历先取来源全量再本地筛选；场内净值分页由 AKShare 处理。输出属于适配器结果，不能恢复上游原始小数精度，也不证明历史公开时点或生产接入已通过。净值币种仍需身份资料核验；ETF 与 LOF 行情能力分别检查。

披露探针支持 `holdings`、`fee`、`dividend-announcements`、`dividend-cumulative`、`dividend-events`、`split-events` 和 `fund-profile`。持仓保留每个报告期的来源行数和 100 行上限标记，不声明完整组合；费率条件保留原文，不直接执行计费；结构化分红和拆分值均标为候选，必须继续用管理人或交易所公告确认单位、业务日期和修订关系。

D0 的最终来源判断保存在 `config/data-source-registry.json`，由 `scripts/data_source_registry.py` 校验和查询。后续 D1/D2 按数据集和 `allowed_uses` 读取该注册表：自动采集只选择 `automatic=true` 的来源；`discovery_only` 结果不能直接写成正式事实；`authoritative_evidence` 用于确认关系、规则及公司行动；`rejected` 和 `not_verified` 不进入采集。每批数据保存实际来源 `id` 和 `policy_version`，用于还原当时采用的来源规则。`runtime/probes/` 只保留本机复查输出，不承担跨阶段配置职责。

## 后端基础服务

D1.1 提供本机 FastAPI 入口和 SQLite 初始化。默认数据库位于 `runtime/database/app.sqlite3`，也可以用 `FUND_ADVICE_DATABASE_PATH` 指定临时或其他本机路径：

```bash
uv sync --locked
uv run --locked uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
```

启动时会按序执行迁移；空库和重复启动都可安全执行，迁移失败会让启动失败并保留失败状态。可访问 [后端健康检查](http://127.0.0.1:8000/health) 查看 schema 版本、SQLite 运行库版本及连接参数。

首次写入或手动刷新真实基金目录：

```bash
uv run --locked --offline python scripts/import_fund_catalog.py
```

导入会先核对 D0 来源注册表，再整批校验并用单个事务写入 SQLite。网页开发服务通过 `/api` 代理读取本地后端；基金库中的“本地真实基金目录”可以按代码或名称搜索，当前只展示已经接入的身份字段。

按基金代码采集真实净值或场内日行情：

```bash
uv run --locked --offline python scripts/import_fund_timeseries.py --code 510050 --timeout 45
uv run --locked --offline python scripts/import_fund_timeseries.py --code 005911 --timeout 45
```

采集器根据真实目录身份选择净值或场内行情，并按 D0 注册表执行来源回退。详情页只展示已写入 SQLite 的序列；没有数据时明确显示尚未采集。

全目录采集使用固定任务名分批执行，任务进度和失败原因保存在 SQLite，重复运行会继续待处理项：

```bash
uv run --locked --offline python scripts/import_fund_batch.py --run-id all-funds-6y-v1 --start-date 20200914 --limit 100
uv run --locked --offline python scripts/import_fund_batch.py --run-id all-funds-6y-v1 --status
```

默认单线程并限制请求频率。失败基金不会自动无限重试，可在核查原因后使用 `--retry-failed`。场内 ETF／LOF 展示 OHLC K 线与成交量，ETF 联接及普通开放式基金展示单位净值和累计净值走势。

## 项目文档

- [产品目标与用户流程](docs/product-goals-and-user-flow.md)
- [整体技术架构与选型依据](docs/technical-architecture.md)
- [界面展示与交互开发文档](docs/ui-interaction-design.md)
- [基金数据与存储开发文档](docs/fund-data-storage-design.md)
- [基金数据与存储分阶段开发与复查计划](docs/fund-data-storage-plan.md)
- [投资分析与建议开发方案](docs/investment-analysis-design.md)
- [投资分析与建议分阶段开发与复查计划](docs/investment-analysis-plan.md)
- [界面测试报告](docs/ui-test-report.md)
- [仓库协作与提交规范](agent.md)

## 远端仓库

[WavesTop/fund_advice](https://github.com/WavesTop/fund_advice)
