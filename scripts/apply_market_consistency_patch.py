#!/usr/bin/env python3
"""Finish the reviewed repair; this one-time script removes itself after verification."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
changes: dict[str, str] = {}


def read(path: str) -> str:
    return changes[path] if path in changes else (ROOT / path).read_text(encoding="utf-8")


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    text = read(path)
    if text.count(old) != count:
        raise RuntimeError(f"Anchor changed: {path}: {text.count(old)} != {count}")
    changes[path] = text.replace(old, new)


def new_file(path: str, text: str) -> None:
    if (ROOT / path).exists():
        raise RuntimeError(f"Refusing to replace an existing new-file target: {path}")
    changes[path] = text


def record_results() -> None:
    plan = ROOT / "docs/investment-analysis-plan.md"
    text = plan.read_text(encoding="utf-8")
    checks = [("锁定依赖安装", "INSTALL_RESULT", None), ("修改前端文件的 Prettier", "FORMAT_RESULT", None),
              ("后端全量 unittest", "BACKEND_RESULT", "backend"), ("前端 TypeScript／构建", "BUILD_RESULT", "build"),
              ("前端全量 Vitest", "FRONTEND_RESULT", "frontend"), ("定向浏览器夹具回归（1440／768／390px）", "BROWSER_RESULT", "browser")]
    rows, errors = [], []
    for label, key, log in checks:
        status = os.environ.get(key, "not_run")
        detail = ""
        if log:
            file = Path(f"/tmp/market-{log}.log")
            content = file.read_text(errors="replace") if file.exists() else ""
            content = re.sub(r"\x1b\[[0-9;]*m", "", content)
            pattern = r"Ran (\d+) tests" if log == "backend" else r"Tests\s+(\d+) passed" if log == "frontend" else r"(\d+) passed" if log == "browser" else None
            matches = re.findall(pattern, content) if pattern else []
            if matches:
                detail = f"；{matches[-1]} 项"
            if status != "success":
                candidates = [line for line in content.splitlines() if re.search(r"FAIL|Error:|error TS|failed|^\s*[>❯]", line)]
                errors.append(f"{label}：\n```text\n" + "\n".join(candidates[-15:])[:6000] + "\n```\n")
        rows.append(f"| {label} | {status}{detail} |")
    block = "| 实际检查 | 结果 |\n| --- | --- |\n" + "\n".join(rows)
    block += "\n| 本机真实数据库、全站浏览器与投资效果验收 | 未运行；定向夹具检查不替代真实数据验收 |\n\n"
    block += f"验证入口：GitHub Actions `Apply and verify market consistency`，运行 `{os.environ.get('GITHUB_RUN_ID', 'unknown')}`；固定输入提交 `{os.environ.get('EXPECTED_SHA', 'unknown')}`，工作区修改经测试后同批发布，含测试源码。\n\n"
    block += "\n".join(errors)
    start = text.index("| 实际检查 | 结果 |", text.index("### 1.5 市场一致性"))
    end = text.index("人工复查：", start)
    text = text[:start] + block + text[end:]
    if all(os.environ.get(key) == "success" for _, key, _ in checks):
        text = text.replace("已实现，验证见下表；待用户真实数据复查", "自动验证通过，待用户真实数据复查")
        text = text.replace("已实现，验证见下表；待浏览器与真实数据联调", "自动验证及定向浏览器夹具通过，待真实数据联调")
    plan.write_text(text, encoding="utf-8")
    Path(__file__).unlink()


if "--record-results" in sys.argv:
    record_results()
    raise SystemExit(0)

# The preceding job ran these reviewed tests but omitted tests/ from git add.
# Recover only the literal reviewed test source, not executable patch logic.
path = "tests/test_market_consistency.py"
if not (ROOT / path).exists():
    source = subprocess.check_output(["git", "show", "e2e2e469efca175d8e101a79dcf3cb4caeeb3820:scripts/apply_market_consistency_patch.py"], text=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "new_file" and len(node.args) == 2:
            if ast.literal_eval(node.args[0]) == path:
                new_file(path, ast.literal_eval(node.args[1]))
                break
    else:
        raise RuntimeError("Reviewed regression test source was not found")
path = "tests/test_backend_d11.py"
if read(path).count(", 10)") == 4:
    replace(path, ", 10)", ", 11)", count=4)
elif read(path).count(", 11)") != 4:
    raise RuntimeError("Migration version assertions changed")

replace("frontend/src/features/market/SectorOpportunities.test.tsx", "funds: [{ code: '510001', name: '测试ETF' }],", "funds: [{ code: '510001', name: '测试ETF', relation_status: 'linked' }],")

# Even a complete refresh failure can commit a failed relation-verification record.
replace("backend/api/main.py", '502, refresh_state)', '502, {"refresh": refresh_state, "current": detail(code)})')
replace("frontend/src/features/market/index.tsx", "        if (!response.ok) throw new Error(data?.error?.message || '真实数据采集失败，请稍后重试。');", '''        if (!response.ok) {
          const current = data?.error?.details?.current as RealFundDetailResponse | undefined;
          if (current?.fund?.code === code) {
            setSeries(current.series ?? null);
            setSeriesOptions(current.series_options ?? (current.series?.kind ? [current.series] : []));
            setRelatedMarket(current.related_market ?? null);
            setRelatedMarkets(current.related_markets ?? []);
          }
          throw new Error(data?.error?.message || '真实数据采集失败，请稍后重试。');
        }''')
# A fallback must not bypass the same monotonic data-date guard.
replace("scripts/import_sector_heat.py", '''                    fetched = fetch_ths_sector_daily(member, ths_code, timeout=timeout)
                    rows = fetched''', '''                    fetched = fetch_ths_sector_daily(member, ths_code, timeout=timeout)
                    if rows and fetched[-1]["date"] < rows[-1]["date"]:
                        raise ValueError("备用源返回旧行情，保留已保存的新行情")
                    rows = fetched''')

new_file("tests/test_market_refresh_state.py", '''import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.related_market import import_related_index, record_relation_check
from backend.storage.timeseries import import_timeseries


class FailedRefreshStateTests(unittest.TestCase):
    def test_error_payload_includes_latest_relation_state_and_preserved_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            import_catalog(settings, [{"code": "510050", "name": "测试ETF", "fund_type": "ETF"}], policy_version="fixture")
            row = {"date": "2026-09-11", "open": "10", "high": "12", "low": "9", "close": "11", "volume": None, "amount": None}
            import_timeseries(settings, "510050", "price", [row], source_id="fixture", policy_version="v1")
            import_related_index(settings, fund_code="510050", index_code="000016", index_name="测试指数", rows=[row], source_id="fixture.index", relation_source_id="fixture.prospectus", evidence_url="https://example.test/evidence")
            app = create_app(settings)
            refresh = next(route.endpoint for route in app.routes if route.path == "/api/funds/{code}/refresh")
            def fail_relation(resolved, code):
                record_relation_check(resolved, code, outcome="failed", error="fixture unavailable")
                raise RuntimeError("fixture unavailable")
            with patch("backend.api.main.refresh_fund_timeseries", side_effect=RuntimeError("NAV unavailable")), patch("backend.api.main.refresh_related_market", side_effect=fail_relation):
                with self.assertRaises(AppError) as caught:
                    refresh("510050")
            error = caught.exception
            self.assertEqual(error.status_code, 502)
            self.assertEqual(error.body.details["refresh"]["status"], "failed")
            current = error.body.details["current"]
            self.assertEqual(current["fund"]["code"], "510050")
            self.assertEqual(current["series"]["rows"], [row])
            self.assertIsNone(current["related_market"])
            self.assertEqual(current["related_markets"][0]["relation_status"], "withheld")


if __name__ == "__main__":
    unittest.main()
''')

new_file("frontend/src/features/market/market-consistency-fixtures.ts", '''/** Synthetic API fixtures only; no source claims or live market requests. */
export function marketDetailFixture() {
  const dates = ['2026-09-11', '2026-09-14', '2026-09-15'];
  const price = {
    kind: 'price' as const, source_id: 'fixture.price', policy_version: 'fixture-unadjusted', updated_at: '2026-09-15T10:00:00Z',
    rows: dates.map((date) => ({ date, open: '10', high: '12', low: '9', close: '11', volume: '100', amount: null })),
  };
  const nav = {
    kind: 'nav' as const, source_id: 'fixture.nav', policy_version: 'fixture-nav', updated_at: '2026-09-15T10:00:00Z',
    rows: [{ date: dates[0], unit_nav: '1.02', accumulated_nav: '1.20' }, { date: dates[2], unit_nav: '1.04', accumulated_nav: '1.22' }],
  };
  const market = {
    code: '000016', name: '一致性测试指数', kind: 'index' as const, source_id: 'fixture.index',
    relation_status: 'linked' as const, relation_source_id: 'fixture.prospectus',
    verified_at: '2026-09-15T10:00:00Z', evidence_url: 'https://example.test/prospectus',
    rows: dates.map((date) => ({ date, open: '100', high: '105', low: '99', close: '103', volume: '1000', amount: null })),
  };
  return {
    fund: { share_id: 'fixture-share', code: '510050', name: '一致性测试ETF', fund_type: 'ETF', source_id: 'fixture.catalog' },
    series: price, series_options: [price, nav], related_market: market, related_markets: [market],
  };
}

export function failedRefreshFixture() {
  const previous = marketDetailFixture();
  return { error: { code: 'fund_refresh_failed', message: '刷新失败，关系核验状态已同步。', details: {
    refresh: { status: 'failed', stages: {} },
    current: { ...previous, related_market: null, related_markets: [{ ...previous.related_market, relation_status: 'withheld', relation_reason: '本轮关系核验失败', rows: [] }] },
  } } };
}
''')

new_file("frontend/src/features/market/RealMarketConsistency.test.tsx", '''import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import { FundDetailPage } from './index';
import { SectorHistory } from './SectorHistory';
import type { SectorOpportunity } from './SectorOpportunities';
import { failedRefreshFixture, marketDetailFixture } from './market-consistency-fixtures';

vi.mock('../../shared/Chart', () => ({ Chart: ({ label, option, linkGroup }: { label: string; option: EChartsOption; linkGroup?: string }) =>
  <div role="img" aria-label={label} data-option={JSON.stringify(option)} data-group={linkGroup} /> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function renderFund() {
  return render(<MemoryRouter initialEntries={['/funds/510050']}><Routes><Route path="/funds/:code" element={<FundDetailPage />} /></Routes></MemoryRouter>);
}
const board: SectorOpportunity = { code: 'BK0001', name: '测试板块', source_id: 'fixture.board', universe_type: 'hot_board', updated_at: '2026-09-15T10:00:00Z', as_of: '2026-09-15', observation_count: 3, funds: [], periods: [] };
function boardHistory() {
  return { ...board, history_source_id: 'fixture.proxy', history_source_code: 'proxy1', history_relation: 'proxy_not_equivalent', collection_error: null, rows: marketDetailFixture().related_market.rows };
}

describe('真实图表与关系状态的一致性', () => {
  it('切换已存储净值，按共同日期保留缺值并共享联动分组', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(marketDetailFixture()));
    renderFund();
    await screen.findByRole('img', { name: '510050真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '基金净值', exact: true }));
    const chart = await screen.findByRole('img', { name: '510050真实净值走势' });
    const option = JSON.parse(chart.getAttribute('data-option')!);
    expect(option.xAxis.data).toEqual(['2026-09-11', '2026-09-14', '2026-09-15']);
    expect(option.series[0].data).toEqual([1.02, null, 1.04]);
    expect(chart.getAttribute('data-group')).toBe(screen.getByRole('img', { name: '000016关联板块真实K线' }).getAttribute('data-group'));
  });
  it('刷新全部失败仍同步核验状态，保留基金行情但不继续绘制失效关联', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(marketDetailFixture())).mockResolvedValueOnce(response(failedRefreshFixture(), 502));
    renderFund();
    await screen.findByRole('img', { name: '000016关联板块真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '更新数据', exact: true }));
    await screen.findByText('刷新失败，关系核验状态已同步。');
    expect(screen.queryByRole('img', { name: '000016关联板块真实K线' })).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: '510050真实K线' })).toBeInTheDocument();
    expect(screen.getByText(/本轮关系核验失败/)).toBeInTheDocument();
  });
  it('部分成功应用已提交序列并说明失败的分项', async () => {
    const result = { ...marketDetailFixture(), refresh: { status: 'partial', stages: { fund_series: { status: 'updated', message: '净值已提交' }, related_market: { status: 'failed', message: '指数暂不可用' } } } };
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(marketDetailFixture())).mockResolvedValueOnce(response(result));
    renderFund();
    await screen.findByRole('img', { name: '510050真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '更新数据', exact: true }));
    expect(await screen.findByText(/本次部分更新成功/)).toHaveTextContent('指数暂不可用');
  });
  it('明确关系但日线为空时解释缺项', async () => {
    const fixture = marketDetailFixture(); fixture.related_market.rows = [];
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(fixture));
    renderFund();
    expect(await screen.findByText('关系已记录，但该指数尚无可用日线。')).toBeInTheDocument();
  });
  it('日线按需请求来源精确身份并说明跨源代理边界', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(boardHistory()));
    render(<SectorHistory item={board} />);
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    expect(await screen.findByRole('img', { name: 'BK0001来源真实日线' })).toBeInTheDocument();
    expect(fetcher.mock.calls[0][0]).toBe('/api/sectors/BK0001/series?source_id=fixture.board&universe_type=hot_board');
    expect(screen.getByText(/跨源参考行情/)).toBeInTheDocument();
  });
  it('错源响应不绘图，重试仍请求原对象', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response({ ...boardHistory(), source_id: 'wrong' })).mockResolvedValueOnce(response(boardHistory()));
    render(<SectorHistory item={board} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await screen.findByText(/行情身份与请求不一致/);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试行情' }));
    await screen.findByRole('img', { name: 'BK0001来源真实日线' });
    expect(fetcher.mock.calls[0][0]).toBe(fetcher.mock.calls[1][0]);
  });
  it('重复行情日期不会静默合并后绘图', async () => {
    const fixture = boardHistory(); fixture.rows.push({ ...fixture.rows[0] });
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(fixture));
    render(<SectorHistory item={board} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('行情字段无效'));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
''')

new_file("frontend/e2e/market-consistency.spec.ts", '''import { expect, test } from '@playwright/test';
import { failedRefreshFixture, marketDetailFixture } from '../src/features/market/market-consistency-fixtures';

for (const width of [1440, 768, 390]) {
  test(`real chart paths with API fixtures at ${width}px`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/funds/510050', (route) => route.fulfill({ json: marketDetailFixture() }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/funds/510050');
    await expect(page.getByRole('heading', { name: '一致性测试ETF', exact: true })).toBeVisible();
    await expect(page.locator('.chart[aria-label="510050真实K线"] svg')).toHaveCount(1);
    await expect(page.locator('.chart[aria-label="000016关联板块真实K线"] svg')).toHaveCount(1);
    await page.getByRole('button', { name: '基金净值', exact: true }).click();
    await expect(page.locator('.chart[aria-label="510050真实净值走势"] svg')).toHaveCount(1);
    await page.getByRole('button', { name: '自定义', exact: true }).click();
    await page.getByLabel('图表起始日期').fill('2026-09-11');
    await page.getByLabel('图表截止日期').fill('2026-09-15');
    await expect(page.getByText('请选择有效且有数据的日期区间。')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test('failed refresh removes unverified index chart without erasing stored fund prices', async ({ page }) => {
  await page.route('**/api/funds/510050', (route) => route.fulfill({ json: marketDetailFixture() }));
  await page.route('**/api/funds/510050/refresh', (route) => route.fulfill({ status: 502, json: failedRefreshFixture() }));
  await page.goto('/funds/510050');
  await expect(page.locator('.chart[aria-label="000016关联板块真实K线"] svg')).toHaveCount(1);
  await page.getByRole('button', { name: '更新数据', exact: true }).click();
  await expect(page.getByText('刷新失败，关系核验状态已同步。')).toBeVisible();
  await expect(page.locator('.chart[aria-label="000016关联板块真实K线"]')).toHaveCount(0);
  await expect(page.locator('.chart[aria-label="510050真实K线"] svg')).toHaveCount(1);
});
''')

replace("docs/fund-data-storage-design.md", '全部无可发布结果返回 502 和逐项原因。', '全部无可发布结果返回 502，`error.details` 包含逐项 `refresh` 及核验后的 `current` 详情；失败检查已落库时，页面必须同步关系状态，不能继续把旧关系显示为当前已核验。')
replace("docs/ui-interaction-design.md", '分项刷新成功后立即展示成功部分，保留失败原因和各自资料日期。', '分项刷新成功后立即展示成功部分，保留失败原因和各自资料日期；全部刷新失败但关系核验状态已改变时，消费错误响应中的同基金当前详情，保留基金行情并停止绘制未通过核验的关联指数。')

for path, content in changes.items():
    if path.endswith('.py'):
        compile(content, path, 'exec')
for path, content in changes.items():
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
print('\n'.join(sorted(changes)))
