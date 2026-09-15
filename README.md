# fund_advice

面向 A 股相关基金和板块的投资建议与决策复盘工具，围绕三个同等重要的产品目标：

- **发现投资机会**：提供较全面的基金库供浏览和搜索，展示基金 K 线图或净值走势、公开披露的股票和集中板块，以及短期、中期和长期的板块机会。
- **辅助投资决策**：结合用户持仓与要求，同时提供三个周期的基金推荐和操作建议。
- **持续复盘与策略改良**：保留系统当时的建议和用户自己的决定，持续对比用户实际结果与系统建议的模拟结果。系统根据复盘识别策略可能存在的问题，提出改良方向，并通过新旧方案对比和后续跟踪检验改良效果。

少数未收录的基金可由用户提供代码，交由系统补充。

## 当前状态

已实现 §3.1 的前端界面与演示交互，并开始接入 §3.2 的真实基金数据。基金库现从本地 FastAPI 与 SQLite 查询真实基金目录；已采集的基金详情展示真实净值或场内日 K 线。“基金与板块 → 板块机会”已接入三周期轻量证据筛查：真实指数行情、经过核验的行业统计与有限估值观察，展示支持、反证和待确认条件。持仓、正式建议、收益复盘、策略及原板块详情仍是演示界面。

**当前不可用于真实投资或记账。** 基金目录以及详情页明确标注来源的净值／K 线来自本地数据库；未采集序列保持空白。其他页面的建议、持仓和收益仍为明确标注的虚构样例，填写的演示记录仅保存在当前浏览器的本地存储。精确核算、模拟、AI 调用和后台执行仍待后续阶段接入。

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

基金详情页没有本地序列时可点击“获取真实数据”，已有数据时可点击“更新数据”；页面默认补充最近约六年的真实净值或场内日行情。“重新评估”只读取已经写入 SQLite 的行情与证据，不联网采集，也不调用 AI。

需要刷新行业池时，先保持网页打开，在另一个终端进入仓库根目录运行：

```bash
uv run --locked --offline python scripts/import_sector_heat.py --industry-only
```

命令完成后刷新“基金与板块 → 板块机会”页面。默认数据库位于 `runtime/database/app.sqlite3`；该目录是本机运行数据，不纳入版本控制。

可在“设置 → 界面验收场景”切换空白、加载、断网、保存失败、资料过期与 AI 意见冲突。请勿填入真实个人资料或密钥；AI 配置只预览发送范围，不会访问外部服务。

板块机会当前以全部可核验行业为主池：完整读取东方财富行业层级目录，再以分类和严格同名关系核对同花顺标准行业日线。概念、风格、地区和资格集合不进入行业主池。页面支持名称／代码搜索，以及成交额、短期、中期、长期走势强度的正反向排序和分页。成交额是活跃度，不是投资价值；走势强度是同一完整交易日、同一观察区间的历史涨跌排名，不是未来上涨概率。资料不足、采集失败、日期不齐的行业不参与当次排名。

页面顶部的三周期优势板块先要求相对强度进入可比较行业前 25%，再核对区间方向、均线位置和最大回撤，区分“趋势领先”“回撤中修复”“领先后回落”和“高波动领先”。短期、中期、长期分别表示未来约 1 周至 1 个月、1 至 3 个月、3 至 6 个月的观察范围；20、60、120 个交易日只是对应的历史走势佐证窗口。板块身份按来源代码、分类体系和业务日期留存版本，成分股按榜单日期保存快照；来源未提供官方权重，因此页面不以总市值替代权重。原有三个真实指数及其行业背景保留在“已有指数观察”，不向其他行业套用未经核实的基金、经营或估值关系。

人工刷新全部可核验行业及日线使用：

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

本轮结果与尚未验收的真实业务范围见[界面测试报告](docs/ui-test-report.md)。

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
