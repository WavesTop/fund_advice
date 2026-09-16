import { expect, test } from '@playwright/test';
import { workbenchCatalog, workbenchDetail, workbenchSector } from '../src/features/market/workbench-fixtures';

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


test('实际打开基金详情后记录，清空搜索恢复浏览卡片，刷新后仍保留', async ({ page }) => {
  await page.route('**/api/funds/000001', async (route) => route.fulfill({ json: {
    fund: { share_id: 'fixture-1', code: '000001', name: '测试基金1', fund_type: 'ETF', source_id: 'fixture.catalog' },
    series: null, related_market: null, related_markets: [],
  } }));
  await page.goto('/funds');
  await expect(page.getByText(/暂无浏览记录/)).toBeVisible();
  await page.getByRole('searchbox').fill('test');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('link', { name: '测试基金1', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: '测试基金1' })).toBeVisible();
  await page.goto('/funds?q=test&page=2');
  await page.getByRole('button', { name: '清空', exact: true }).click();
  await expect(page.getByRole('searchbox')).toHaveValue('');
  await expect(page.getByRole('list', { name: '基金浏览记录' }).getByRole('listitem')).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole('list', { name: '基金浏览记录' }).getByRole('listitem')).toHaveCount(1);
});

test('只保留重新评估，连续五次POST显示各次返回结果', async ({ page }) => {
  let posts = 0;
  const initial = { ...workbenchDetail(), items: [workbenchSector()] };
  await page.route('**/api/sectors/opportunities', async (route) => route.fulfill({ json: initial }));
  await page.route('**/api/sectors/refresh', async (route) => {
    expect(route.request().method()).toBe('POST'); posts += 1;
    const item = { ...workbenchSector(), name: `本轮行业${posts}` };
    await route.fulfill({ json: { refresh: { target: 'sectors', status: 'success', message: `完成第${posts}次` },
      evaluation: { ...initial, items: [item] }, api_contract: 'market-workbench-v2' } });
  });
  await page.goto('/sectors');
  await expect(page.getByRole('table', { name: '板块三周期历史表现与研究证据对照' })).toBeVisible();
  await expect(page.getByRole('button', { name: '获取最新行业数据' })).toHaveCount(0);
  for (let round = 1; round <= 5; round += 1) {
    await page.getByRole('button', { name: '重新评估', exact: true }).click();
    await expect(page.getByRole('rowheader', { name: new RegExp(`本轮行业${round}`) }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: '重新评估', exact: true })).toBeEnabled();
  }
  expect(posts).toBe(5);
});
