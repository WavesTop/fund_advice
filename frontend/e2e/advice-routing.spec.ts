import { expect, test } from '@playwright/test';
import { researchFixture } from '../src/features/advice/research-fixtures';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/portfolio/accounts', (route) => route.fulfill({
    json: { items: [{ account_id: 'acc-1', name: '测试账户', base_currency: 'CNY' }] },
  }));
  await page.route('**/api/v1/strategy/recommendations/latest?**', (route) => route.fulfill({
    json: {
      item: null,
      freshness: {
        state: 'unavailable',
        reason_code: 'recommendation_missing',
        reason: '尚未生成建议',
        checked_cutoff: '2026-09-21',
        stored_input_hash: null,
        current_input_hash: null,
        changed_inputs: [],
      },
    },
  }));
  await page.route('**/api/v1/strategy/decisions?**', (route) => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/sectors/opportunities', (route) => route.fulfill({ json: researchFixture() }));
});

test('advice defaults to formal recommendations and keeps research and demo explicit', async ({ page }) => {
  await page.goto('/advice');
  await expect(page.getByRole('heading', { name: '建议与决策' })).toBeVisible();
  await expect(page.getByText('尚未生成正式策略建议')).toBeVisible();
  await expect(page.getByRole('link', { name: '查看研究依据' })).toBeVisible();

  await page.goto('/advice?tab=research');
  await expect(page.getByRole('heading', { name: '投资分析与建议' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '短期', exact: true })).toBeVisible();

  await page.goto('/advice?mode=demo');
  await expect(page.getByText('三周期界面示例 · 未生成真实投资建议')).toBeVisible();
  await expect(page.getByRole('button', { name: '记录决定', exact: true }).first()).toBeVisible();
});
