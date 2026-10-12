import { expect, test } from '@playwright/test';

const period = (id: 'short' | 'medium' | 'long') => ({
  id,
  name: id === 'short' ? '短期' : id === 'medium' ? '中期' : '长期',
  range: '观察期',
  lookback_sessions: id === 'short' ? 20 : id === 'medium' ? 60 : 120,
  status: 'strong',
  label: '上涨走势',
  reason: '测试走势',
  return_pct: 2,
  ma_bias_pct: 1,
  max_drawdown_pct: -3,
  risk: 'normal',
  opportunity: {
    status: 'watch',
    label: '观察',
    summary: '测试摘要',
    supports: [],
    challenges: [],
    conditions: [],
    missing: [],
  },
});

const rows = Array.from({ length: 90 }, (_, index) => {
  const close = 100 + index * 0.4 + Math.sin(index / 4) * 2;
  const day = new Date(Date.UTC(2026, 5, 1 + index)).toISOString().slice(0, 10);
  return {
    date: day,
    open: String(close - 0.3),
    high: String(close + 1),
    low: String(close - 1),
    close: String(close),
  };
});

test('sector library renders kline and four indicator charts', async ({ page }) => {
  await page.route('**/api/sectors/opportunities', (route) => route.fulfill({
    json: {
      method_version: 'sector-horizons-v1',
      generated_at: '2026-09-21T10:00:00+08:00',
      items: [
        {
          code: 'BK0001',
          name: '测试板块一',
          source_id: 'sector_daily.ths',
          universe_type: 'hot_board',
          updated_at: '2026-09-21T10:00:00+08:00',
          as_of: '2026-08-29',
          observation_count: 90,
          heat_rank: 1,
          heat_value: 100000000,
          funds: [],
          periods: [period('short'), period('medium'), period('long')],
        },
        {
          code: 'BK0002',
          name: '测试板块二',
          source_id: 'sector_daily.ths',
          universe_type: 'hot_board',
          updated_at: '2026-09-21T10:00:00+08:00',
          as_of: '2026-08-29',
          observation_count: 90,
          heat_rank: 2,
          heat_value: 90000000,
          funds: [],
          periods: [period('short'), period('medium'), period('long')],
        },
      ],
    },
  }));
  await page.route('**/api/sectors/*/series?**', (route) => route.fulfill({
    json: {
      code: route.request().url().includes('BK0002') ? 'BK0002' : 'BK0001',
      source_id: 'sector_daily.ths',
      universe_type: 'hot_board',
      history_source_id: 'sector_daily.ths',
      history_source_code: 'BK0001',
      history_relation: 'native_source',
      updated_at: '2026-09-21T10:23:45+08:00',
      as_of: '2026-08-29',
      collection_error: null,
      rows,
    },
  }));
  await page.goto('/sectors?tab=library');
  await expect(page.getByRole('heading', { name: '板块库' })).toBeVisible();
  await expect(page.getByRole('button', { name: /测试板块一/ })).toBeVisible();
  await expect(page.getByLabel('测试板块一 K线以及 MACD、KDJ、RSI、WR 四类常用指标图')).toBeVisible();
  await expect(page.getByLabel('技术阶段筛选')).toBeVisible();
  await expect(page.getByRole('button', { name: /测试板块一/ })).toContainText('行情查询 2026-09-21 10:23:45');
  await expect(page.getByRole('button', { name: /测试板块二/ })).toContainText('行情查询 2026-09-21 10:23:45');
  await expect(page.locator('.sector-library-chart')).toContainText('行情查询 2026-09-21 10:23:45');
  await expect(page.getByText(/15:00:00（收盘）/)).toHaveCount(0);
});
