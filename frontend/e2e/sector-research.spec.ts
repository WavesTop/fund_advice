import { expect, test } from '@playwright/test';
import { resolve } from 'node:path';

for (const [name, width, height] of [
  ['desktop', 1440, 1000],
  ['mobile', 390, 844],
] as const) {
  test(`sector research ${name}: horizons, frozen chart, empty selections, losses and export`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    const errors: string[] = [];
    const sectorRequests: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      if (/\/api\/.*sector/.test(request.url())) sectorRequests.push(request.url());
    });
    await page.goto('/sectors?view=research&rule=maximin_top10');
    await expect(page.getByRole('heading', { name: '板块机会 · 趋势研究' })).toBeVisible();
    await expect(page.getByText('候选与价格依据：2026-10-13')).toBeVisible();
    const chart = page.getByRole('img', { name: /短期冻结价格路径/ });
    await expect(chart.locator('svg')).toBeVisible();
    expect(await chart.locator('svg path').count()).toBeGreaterThan(3);
    await expect(page.getByRole('region', { name: '连续账户历史检验' })).toContainText('+115.04%');
    await expect(page.getByRole('region', { name: '新入场收益检验' })).toContainText('-10.17%');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({
      path: resolve('../runtime/verification', `sector-opportunity-trial-v1-${name}.png`),
      fullPage: true,
    });
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出研究快照' }).click();
    expect((await downloadPromise).suggestedFilename()).toBe(
      'sector-opportunity-trial-2026-10-13.json',
    );
    await page.getByRole('tab', { name: /中期/ }).click();
    await expect(page.getByRole('region', { name: '连续账户历史检验' })).toContainText('+73.00%');
    await page.getByLabel('研究规则').selectOption('price_state');
    await expect(
      page.getByRole('region', { name: '三周期研究候选表' }).getByRole('row'),
    ).toHaveCount(10);
    await expect(page.getByRole('note', { name: '价格方向与模型筛选诊断' })).toContainText(
      '中期价格稳定上行 9 个',
    );
    await expect(page.getByRole('region', { name: '价格上行状态历史检验' })).toContainText(
      '-0.49%',
    );
    await expect(page.getByText(/不能当作这份价格观察名单的收益/)).toBeVisible();
    await expect(page.getByRole('region', { name: '连续账户历史检验' })).not.toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({
      path: resolve('../runtime/verification', `sector-price-direction-v1-${name}.png`),
      fullPage: true,
    });
    await page.getByText('旧模型连续账户参考 · 与价格观察名单不同', { exact: true }).click();
    await expect(page.getByRole('region', { name: '连续账户历史检验' })).toBeVisible();
    await page.getByText('旧模型连续账户参考 · 与价格观察名单不同', { exact: true }).click();
    await page.getByRole('tab', { name: /长期/ }).click();
    await expect(page.getByText(/当日没有板块处于稳定上行或趋势增强状态/)).toBeVisible();
    await page.getByLabel('研究规则').selectOption('maximin_top10');
    await expect(page.getByRole('region', { name: '连续账户历史检验' })).toContainText('+64.70%');
    await page.getByLabel('研究规则').selectOption('upward_quality');
    await expect(page.getByText(/当日没有板块满足该规则/)).toBeVisible();
    await expect(page.getByRole('img', { name: /冻结价格路径/ })).toHaveCount(0);
    await page.getByText('牛熊阶段与覆盖缺口', { exact: true }).click();
    await expect(page.getByRole('region', { name: '牛熊阶段检验' })).toContainText('历史不足');
    await page.getByLabel('研究规则').selectOption('maximin_top10');
    await page.getByText('费用、复核时点与执行敏感性 · 全部 9 组', { exact: true }).click();
    await expect(page.getByRole('region', { name: '敏感性检验' }).getByRole('row')).toHaveCount(10);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    expect(errors).toEqual([]);
    expect(sectorRequests).toEqual([]);
  });
}
