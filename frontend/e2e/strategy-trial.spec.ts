import { expect, test } from '@playwright/test';
import { resolve } from 'node:path';

for (const [name, width, height] of [
  ['desktop', 1440, 1000],
  ['mobile', 390, 844],
] as const) {
  test(`research trial ${name}: chart, negative results, periods and export`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('/trial');
    await expect(page.getByRole('heading', { name: '策略研究试用', exact: true })).toBeVisible();
    await expect(page.getByRole('radio', { name: '分散配置对照' })).toBeChecked();
    const chart = page.getByRole('img', { name: '12主题池两种策略的总资产历史曲线' });
    await expect(chart.locator('svg')).toBeVisible();
    expect(await chart.locator('svg path').count()).toBeGreaterThan(5);
    await page.getByRole('radio', { name: '趋势加权试验' }).click();
    await expect(page.getByRole('radio', { name: '趋势加权试验' })).toBeChecked();
    await expect(page).toHaveURL(/policy=fixed20/);
    await expect(page.locator('.trial-metrics')).toContainText('+41.98%');
    await expect(page.locator('.trial-metrics')).toContainText('10.79%');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({
      path: resolve('../runtime/verification', `strategy-trial-v1-${name}.png`),
      fullPage: true,
    });

    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出快照' }).click();
    expect((await downloadPromise).suggestedFilename()).toBe(
      'strategy-trial-all13-2026-10-12.json',
    );
    await page.getByRole('radio', { name: '牛熊区间' }).check();
    await page.getByLabel('市场阶段').selectOption('bear');
    const table = page.getByRole('region', { name: '分段收益与回撤表' });
    await expect(table).toContainText('历史不足');
    await page.getByLabel('历史基金池').selectOption('long4');
    await expect(table).not.toContainText('历史不足');
    await expect(table).toContainText('2018-01 至 2018-12');
    await page.getByRole('radio', { name: '回撤', exact: true }).check();
    await expect(
      page.getByRole('img', { name: '长期宽基池两种策略的回撤历史曲线' }).locator('svg'),
    ).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    expect(errors).toEqual([]);
  });
}
