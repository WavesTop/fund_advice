import { expect, test } from '@playwright/test';
import { workbenchCatalog, workbenchDetail } from '../src/features/market/workbench-fixtures';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/funds?**', async (route) => route.fulfill({ json: workbenchCatalog(route.request().url()) }));
  await page.route('**/api/sectors/980017?**', async (route) => route.fulfill({ json: workbenchDetail() }));
});

test('基金卡片翻页、真实板块详情和返回查询', async ({ page }) => {
  await page.goto('/funds?q=test');
  const cards = page.getByRole('list', { name: '基金搜索结果' }).getByRole('listitem');
  await expect.poll(() => cards.count()).toBeGreaterThanOrEqual(6);
  expect(await cards.count()).toBeLessThanOrEqual(8);
  await page.getByRole('link', { name: '查看测试关联指数详情' }).click();
  await expect(page.getByRole('heading', { level: 1, name: '测试关联指数' })).toBeVisible();
  await expect(page).toHaveURL(/source_id=fixture.index/);
  await page.getByRole('link', { name: '返回来源列表' }).click();
  await expect(page.getByRole('searchbox')).toHaveValue('test');
  await page.getByRole('button', { name: '本地目录下一页' }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByRole('link', { name: '测试基金1', exact: true })).toHaveCount(0);
});

test('宽屏8张与窄屏6张，卡片不造成整页横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/funds?q=test');
  await expect(page.getByRole('list', { name: '基金搜索结果' }).getByRole('listitem')).toHaveCount(8);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('list', { name: '基金搜索结果' }).getByRole('listitem')).toHaveCount(6);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test('更新目录确实POST，失败可见且保留搜索结果', async ({ page }) => {
  let posts = 0;
  await page.route('**/api/funds/catalog/refresh', async (route) => {
    expect(route.request().method()).toBe('POST');
    posts += 1;
    await route.fulfill({ status: 502, json: { error: { message: '测试数据源不可用' } } });
  });
  await page.goto('/funds?q=test');
  await expect(page.getByRole('link', { name: '测试基金1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '更新基金目录' }).click();
  await expect(page.getByRole('alert')).toContainText('测试数据源不可用');
  await expect(page.getByRole('link', { name: '测试基金1', exact: true })).toBeVisible();
  expect(posts).toBe(1);
});
