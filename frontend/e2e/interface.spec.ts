import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { initialState } from '../src/shared/data';

const key = 'fund-advice.demo.v1';
async function stored(page: Page) {
  return page.evaluate((storageKey) => JSON.parse(localStorage.getItem(storageKey) || 'null'), key);
}
async function recordDecision(page: Page) {
  await page.goto('/advice');
  await page.getByRole('button', { name: '记录决定', exact: true }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('radio', { name: /全部采纳/ }).check();
  await dialog.getByLabel('自己的理由与计划').fill('浏览器验收：先观察，不自动记账');
  await dialog.getByRole('button', { name: '保存个人决定' }).click();
  await expect(page.getByRole('dialog', { name: '个人决定 · 当时记录' })).toBeVisible();
}
async function fillTrade(page: Page) {
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('交易渠道', { exact: true }).selectOption('第三方销售平台');
  await dialog.getByRole('button', { name: '下一步' }).click();
  await dialog.getByLabel('申请 / 发生日期').fill('2026-09-10');
  await dialog.getByLabel('金额 / 已记录成本（元）').fill('1000.00');
  await dialog.getByRole('button', { name: '下一步' }).click();
  await dialog.getByRole('button', { name: '保存演示记录' }).click();
  await expect(dialog).not.toBeVisible();
}

test('real fund search and browser back preserve context', async ({ page }) => {
  await page.route('**/api/funds?*', async (route) => {
    const query = new URL(route.request().url()).searchParams.get('q');
    await route.fulfill({ json: query ? { items: [{ share_id: '005911', code: '005911', name: '广发双擎升级混合A', fund_type: '混合型-偏股', source_id: 'fund_catalog.eastmoney' }], page: 1, page_size: 20, total: 1, catalog_total: 27843, updated_at: '2026-09-13T10:00:00Z' } : { items: [], page: 1, page_size: 20, total: 27843, catalog_total: 27843, updated_at: '2026-09-13T10:00:00Z' } });
  });
  await page.route('**/api/funds/005911', (route) => route.fulfill({ json: { fund: { share_id: '005911', code: '005911', name: '广发双擎升级混合A', fund_type: '混合型-偏股', source_id: 'fund_catalog.eastmoney' }, series: null } }));
  await page.goto('/funds');
  await expect(page.getByText('真实基金数据', { exact: true })).toBeVisible();
  await expect(page.getByText(/已收录 27843 条/)).toBeVisible();
  await page.getByLabel('搜索本地真实基金').fill('005911');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('link', { name: '广发双擎升级混合A' }).click();
  await expect(page).toHaveURL(/funds\/005911/);
  await page.goBack();
  await expect(page.getByLabel('搜索本地真实基金')).toHaveValue('005911');
  await expect(page.locator('tbody tr')).toHaveCount(1);
});

test('an empty real fund search does not invent an identity', async ({ page }) => {
  await page.route('**/api/funds?*', (route) => route.fulfill({ json: { items: [], page: 1, page_size: 20, total: 0, catalog_total: 27843, updated_at: '2026-09-13T10:00:00Z' } }));
  await page.goto('/funds');
  await page.getByLabel('搜索本地真实基金').fill('999998');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText(/没有找到“999998”对应的基金/)).toBeVisible();
  await expect(page.locator('tbody tr')).toHaveCount(0);
});

test('fund detail renders only a persisted real price series', async ({ page }) => {
  await page.route('**/api/funds/510050', (route) => route.fulfill({ json: { fund: { share_id: '510050', code: '510050', name: '上证50ETF华夏', fund_type: '指数型-股票', source_id: 'fund_catalog.eastmoney' }, series: { kind: 'price', source_id: 'exchange_daily.sina', policy_version: 'd0-test', updated_at: '2026-09-13T10:00:00Z', rows: [{ date: '2026-09-11', open: '2.900', high: '3.100', low: '2.800', close: '3.000', volume: '100', amount: '300' }] } } }));
  await page.goto('/funds/510050');
  await expect(page.getByRole('heading', { name: '真实 K 线' })).toBeVisible();
  await expect(page.getByRole('img', { name: '510050真实K线' })).toBeVisible();
  await expect(page.getByText(/模拟行情|演示行情/)).toHaveCount(0);
});

test('a decision does not change transactions; later linked trade persists after reload', async ({
  page,
}) => {
  await recordDecision(page);
  const decisionState = await stored(page);
  expect(decisionState.decisions).toHaveLength(1);
  expect(decisionState.transactions).toHaveLength(0);
  await page.getByRole('link', { name: '记录实际操作', exact: true }).click();
  await fillTrade(page);
  const tradeState = await stored(page);
  expect(tradeState.transactions).toHaveLength(1);
  expect(tradeState.transactions[0].decisionIds).toEqual([tradeState.decisions[0].id]);
  expect(tradeState.transactions[0].amount).toBe('1000.00');
  await page.reload();
  await expect(page.getByText('1 笔记录', { exact: true })).toBeVisible();
  await page.goto('/advice?tab=history');
  await expect(page.getByRole('cell', { name: /^已关联 1 笔操作/ })).toBeVisible();
});

test('failed save preserves the entered decision and does not create a record', async ({
  page,
}) => {
  await page.addInitScript(({ key, state }) => localStorage.setItem(key, JSON.stringify(state)), {
    key,
    state: { ...initialState, scenario: 'error' },
  });
  await page.goto('/advice');
  await page.getByRole('button', { name: '记录决定', exact: true }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('radio', { name: /部分采纳/ }).check();
  await dialog.getByLabel('调整后的计划金额（元，必填）').fill('300');
  await dialog.getByLabel('调整理由与具体计划（必填）').fill('先保留三百元的演示计划');
  await dialog.getByRole('button', { name: '保存个人决定' }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('调整后的计划金额（元，必填）')).toHaveValue('300');
  await expect(page.getByRole('status')).toContainText('保存失败');
  expect((await stored(page)).decisions).toHaveLength(0);
});

test('dirty dialog asks before closing and retains input when cancelled', async ({ page }) => {
  await page.goto('/funds');
  await page.getByRole('button', { name: '补充基金', exact: true }).click();
  await page.getByRole('dialog').getByLabel('基金代码', { exact: true }).fill('123456');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByLabel('基金代码', { exact: true })).toHaveValue('123456');
  page.once('dialog', (dialog) => dialog.accept());
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '补充基金', exact: true })).toBeFocused();
});

test('unknown cash stays unknown and dirty conditions can block navigation', async ({ page }) => {
  await page.goto('/portfolio');
  await expect(page.getByText('未知不等于零，总资产暂不可用')).toBeVisible();
  await page.getByRole('button', { name: '投资条件', exact: true }).click();
  await page.getByLabel('已知可用现金（元）').fill('2000');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('link', { name: '基金与板块', exact: true }).click();
  await expect(page).toHaveURL(/tab=conditions/);
  await expect(page.getByLabel('已知可用现金（元）')).toHaveValue('2000');
  await page.getByRole('button', { name: '保存投资条件', exact: true }).click();
  await page.reload();
  await expect(page.getByLabel('已知可用现金（元）')).toHaveValue('2000');
});

test('noncomparable returns expose no misleading difference; personal returns remain unavailable', async ({
  page,
}) => {
  await page.goto('/review');
  await expect(page.getByText('-65.00 元 ↗', { exact: true })).toBeVisible();
  await page.getByLabel('比较条件').selectOption('different');
  await expect(page.getByRole('button', { name: '暂不可比' })).toHaveCount(3);
  await expect(page.locator('.review-mini-chart')).toHaveCount(3);
  await page.getByLabel('展示虚构比较样例').uncheck();
  await expect(page.getByRole('heading', { name: '还没有可核算的收益结果' })).toBeVisible();
});

test('strategy enable and restore require confirmation and retain adoption history', async ({
  page,
}) => {
  await page.goto('/review?tab=strategy');
  await page.getByRole('button', { name: '演示启用 v1.1' }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: '确认启用' })).toBeDisabled();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: '确认启用' }).click();
  await page.getByRole('button', { name: '演示恢复 v1.0' }).click();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: '确认恢复' }).click();
  const state = await stored(page);
  expect(state.strategyVersion).toBe('v1.0');
  expect(state.strategyHistory.map((event: { version: string }) => event.version)).toEqual([
    'v1.1',
    'v1.0',
  ]);
});

test('transaction corrections and revocations append history without rewriting originals', async ({
  page,
}) => {
  await page.goto('/portfolio?action=trade&fund=510300');
  await fillTrade(page);
  const original = (await stored(page)).transactions[0];
  await page.goto(`/portfolio?tab=transactions&transaction=${original.id}`);
  await page.getByRole('dialog').getByRole('button', { name: '更正记录', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '下一步' }).click();
  await dialog.getByLabel('金额 / 已记录成本（元）').fill('1200.00');
  await dialog.getByRole('button', { name: '下一步' }).click();
  await dialog.getByLabel('更正原因（必填）').fill('核对原单据后修正金额');
  await dialog.getByRole('button', { name: '保存更正记录' }).click();
  await expect(dialog).not.toBeVisible();
  const corrected = await stored(page);
  expect(corrected.transactions).toHaveLength(2);
  expect(corrected.transactions[0]).toEqual(original);
  expect(corrected.transactions[1]).toMatchObject({ amount: '1200.00', corrects: original.id });
  await page.goto(`/portfolio?tab=transactions&transaction=${corrected.transactions[1].id}`);
  await page.getByRole('dialog').getByRole('button', { name: '撤销误录', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认撤销误录' })).toBeDisabled();
  await dialog.getByLabel('撤销原因').fill('确认是测试误录，保留审计轨迹');
  await dialog.getByRole('button', { name: '确认撤销误录' }).click();
  await expect(dialog).not.toBeVisible();
  await page.reload();
  const cancelled = await stored(page);
  expect(cancelled.transactions).toHaveLength(3);
  expect(cancelled.transactions.slice(0, 2)).toEqual(corrected.transactions);
  expect(cancelled.transactions[2]).toMatchObject({
    status: '已撤销',
    corrects: corrected.transactions[1].id,
  });
});

test('fee editor blocks overlapping boundaries and preserves unknown rather than zero fees', async ({
  page,
}) => {
  await page.goto('/portfolio?tab=fees');
  await page.getByRole('button', { name: '＋ 录入交易规则' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('费率适用基金').selectOption('000001');
  await dialog.getByLabel('费率适用渠道').fill('演示销售渠道');
  await dialog.getByLabel('区间1下界').fill('0');
  await dialog.getByLabel('区间1上界').fill('7');
  await dialog.getByLabel('区间1费率').fill('1.5');
  await dialog.getByRole('button', { name: '＋ 添加持有期限区间' }).click();
  await dialog.getByLabel('区间2下界').fill('6');
  await dialog.getByRole('button', { name: '保存演示规则' }).click();
  await expect(dialog.getByRole('alert')).toContainText('重叠');
  expect((await stored(page))?.feeRules ?? []).toHaveLength(0);
  await dialog.getByLabel('区间2下界').fill('7');
  await dialog.getByRole('button', { name: '保存演示规则' }).click();
  await expect(dialog).not.toBeVisible();
  const state = await stored(page);
  expect(state.feeRules).toHaveLength(1);
  expect(state.feeRules[0].confirmed).toBe(false);
  expect(state.feeRules[0].tiers[1].rate).toBe('');
  await page.reload();
  await expect(page.getByText('未知', { exact: true })).toBeVisible();
});

for (const width of [1440, 768, 390]) {
  test(`three periods and major pages remain usable at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    for (const route of [
      '/funds',
      '/funds/510300',
      '/sectors',
      '/advice',
      '/portfolio',
      '/review',
      '/settings',
    ]) {
      await page.goto(route);
      await expect(page.locator('main h1')).toBeVisible();
      const layout = await page.evaluate(() => ({
        width: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(layout.scrollWidth, `${route} overflows at ${width}px`).toBeLessThanOrEqual(
        layout.width + 1,
      );
      if (route === '/advice') {
        for (const name of ['短期', '中期', '长期'])
          await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
      }
      if (['/funds', '/advice', '/review'].includes(route))
        await page.screenshot({
          path: testInfo.outputPath(`${route.slice(1)}-${width}.png`),
          fullPage: true,
        });
    }
    if (width < 960) {
      await page.getByRole('button', { name: '展开导航' }).click();
      await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible();
      await page.getByRole('link', { name: '我的持仓', exact: true }).click();
      await expect(page).toHaveURL(/portfolio/);
      await expect(page.getByRole('button', { name: '展开导航' })).toBeVisible();
    }
  });
}

test('core pages meet WCAG A/AA automated checks and render without JS errors', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  const violations: {
    route: string;
    rule: string;
    nodes: { target: unknown; reason?: string }[];
  }[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const route of [
    '/funds',
    '/funds/510300',
    '/advice',
    '/portfolio',
    '/review',
    '/settings',
  ]) {
    await page.goto(route);
    await expect(page.locator('main h1')).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    await testInfo.attach(`accessibility-${route.replaceAll('/', '-')}`, {
      body: JSON.stringify(results.violations, null, 2),
      contentType: 'application/json',
    });
    for (const violation of results.violations)
      violations.push({
        route,
        rule: violation.id,
        nodes: violation.nodes.map((node) => ({
          target: node.target,
          reason: node.failureSummary,
        })),
      });
  }
  expect(violations).toEqual([]);
  expect(errors).toEqual([]);
});
