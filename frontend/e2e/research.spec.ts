import { expect, test } from '@playwright/test';
import { researchFixture } from '../src/features/advice/research-fixtures';

for (const width of [1440, 768, 390]) {
  test(`real research keeps three horizons and no page overflow at ${width}px`, async ({ page }) => {
    await page.route('**/api/sectors/opportunities', (route) => route.fulfill({ json: researchFixture() }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/advice');
    for (const name of ['短期', '中期', '长期']) await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '记录决定', exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.getByRole('link', { name: '查看三周期研究', exact: true }).click();
    await expect(page).toHaveURL(/sector=BK_TEST/);
    await expect(page).toHaveURL(/source=fixture.board/);
    await expect(page.getByText(/暂无来源与核验时间完整的基金映射/)).toBeVisible();
  });
}

test('unknown real fund never becomes an example fund', async ({ page }) => {
  await page.route('**/api/sectors/opportunities', (route) => route.fulfill({ json: researchFixture() }));
  await page.route('**/api/funds/999999', (route) => route.fulfill({ status: 404, json: {} }));
  await page.goto('/advice?fund=999999');
  await expect(page.getByText(/本地真实目录没有基金 999999/)).toBeVisible();
  await expect(page.getByRole('button', { name: '记录决定', exact: true })).toHaveCount(0);
});
