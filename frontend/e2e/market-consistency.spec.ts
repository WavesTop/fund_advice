import { expect, test } from '@playwright/test';
import {
  failedRefreshFixture,
  marketDetailFixture,
} from '../src/features/market/market-consistency-fixtures';

for (const width of [1440, 768, 390]) {
  test(`real chart paths with API fixtures at ${width}px`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/funds/510050', (route) =>
      route.fulfill({ json: marketDetailFixture() }),
    );
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
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
      true,
    );
    expect(errors).toEqual([]);
  });
}

test('failed refresh removes unverified index chart without erasing stored fund prices', async ({
  page,
}) => {
  await page.route('**/api/funds/510050', (route) =>
    route.fulfill({ json: marketDetailFixture() }),
  );
  await page.route('**/api/funds/510050/refresh', (route) =>
    route.fulfill({ status: 502, json: failedRefreshFixture() }),
  );
  await page.goto('/funds/510050');
  await expect(page.locator('.chart[aria-label="000016关联板块真实K线"] svg')).toHaveCount(1);
  await page.getByRole('button', { name: '更新数据', exact: true }).click();
  await expect(page.getByText('刷新失败，关系核验状态已同步。')).toBeVisible();
  await expect(page.locator('.chart[aria-label="000016关联板块真实K线"]')).toHaveCount(0);
  await expect(page.locator('.chart[aria-label="510050真实K线"] svg')).toHaveCount(1);
});
