# fund_advice

面向 A 股相关基金和板块的投资建议与决策复盘工具，围绕三个同等重要的产品目标：

- **发现投资机会**：提供较全面的基金库供浏览和搜索，展示基金 K 线图或净值走势、公开披露的股票和集中板块，以及短期、中期和长期的板块机会。
- **辅助投资决策**：结合用户持仓与要求，同时提供三个周期的基金推荐和操作建议。
- **持续复盘与策略改良**：保留系统当时的建议和用户自己的决定，持续对比用户实际结果与系统建议的模拟结果。系统根据复盘识别策略可能存在的问题，提出改良方向，并通过新旧方案对比和后续跟踪检验改良效果。

少数未收录的基金可由用户提供代码，交由系统补充。

## 当前状态

已实现 §3.1 的前端界面与演示交互：基金与板块、持仓与交易、三周期建议与决定、收益复盘、策略版本及设置。采用 React + TypeScript + Vite + ECharts，后端保留 Python + FastAPI、SQLite 与本机任务进程的架构。

**当前为界面验收版，不可用于真实投资或记账。** 行情、基金身份、建议及收益曲线均为明确标注的虚构样例。填写的演示记录仅保存在当前浏览器的本地存储，可导出／导入 JSON；它不是 SQLite 账本或成套备份。真实数据采集、精确核算、模拟、AI 调用和后台执行待 §3.2–§3.6 接入。

## 本地查看

首次安装需联网及可用的 npm；建议系统安装 Node.js 24 LTS。项目另固定本地 Node.js 24 运行依赖供 npm 脚本使用，不修改系统 Node.js。

```bash
cd frontend
npm ci
npm run dev
```

浏览器打开 [本地界面](http://127.0.0.1:5173)。仅监听本机地址；停止时在启动终端按 Ctrl+C。

可在“设置 → 界面验收场景”切换空白、加载、断网、保存失败、资料过期与 AI 意见冲突。请勿填入真实个人资料或密钥；AI 配置只预览发送范围，不会访问外部服务。

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

## 项目文档

- [产品目标与用户流程](docs/product-goals-and-user-flow.md)
- [整体技术架构与选型依据](docs/technical-architecture.md)
- [界面展示与交互开发文档](docs/ui-interaction-design.md)
- [界面测试报告](docs/ui-test-report.md)
- [仓库协作与提交规范](agent.md)

## 远端仓库

[WavesTop/fund_advice](https://github.com/WavesTop/fund_advice)
