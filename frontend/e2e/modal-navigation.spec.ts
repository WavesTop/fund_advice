import { test, expect } from '@playwright/test';

test('dirty modal preserves input when browser back is cancelled and leaves when confirmed', async ({
  page,
}) => {
  await page.goto('/sectors');
  await page.getByRole('link', { name: '基金库', exact: true }).click();
  await page.getByRole('button', { name: '补充基金', exact: true }).click();
  await page.getByRole('dialog').getByLabel('基金代码', { exact: true }).fill('123456');
  let prompts = 0;
  page.once('dialog', async (dialog) => {
    prompts++;
    await dialog.dismiss();
  });
  await page.evaluate(() => history.back());
  await expect.poll(() => prompts).toBe(1);
  await expect(page).toHaveURL(/\/funds$/);
  await expect(page.getByRole('dialog').getByLabel('基金代码', { exact: true })).toHaveValue(
    '123456',
  );
  page.once('dialog', async (dialog) => {
    prompts++;
    await dialog.accept();
  });
  await page.evaluate(() => history.back());
  await expect(page).toHaveURL(/\/sectors$/);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(prompts).toBe(2);
});

test('a modal destination link does not close the form when SPA navigation is cancelled', async ({
  page,
}) => {
  await page.goto('/funds');
  await page.getByRole('button', { name: '补充基金', exact: true }).click();
  const modal = page.getByRole('dialog');
  await modal.getByLabel('基金代码', { exact: true }).fill('000002');
  await modal.getByRole('button', { name: '查询演示身份' }).click();
  page.once('dialog', (dialog) => dialog.dismiss());
  await modal.getByRole('link', { name: '确认身份，进入详情' }).click();
  await expect(page).toHaveURL(/\/funds$/);
  await expect(modal.getByLabel('基金代码', { exact: true })).toHaveValue('000002');
  page.once('dialog', (dialog) => dialog.accept());
  await modal.getByRole('link', { name: '确认身份，进入详情' }).click();
  await expect(page).toHaveURL(/\/funds\/000002$/);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('C类');
});

test('page and transaction modal share one blocker and saving clears the final guard', async ({
  page,
}, testInfo) => {
  const warnings: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'warning') warnings.push(message.text());
  });
  await page.goto('/funds');
  await page.getByRole('link', { name: '我的持仓', exact: true }).click();
  await page.getByRole('button', { name: '投资条件', exact: true }).click();
  await page.getByLabel('已知可用现金（元）').fill('2000');
  await page.getByRole('button', { name: '＋ 记录实际交易', exact: true }).click();
  await page.getByRole('dialog').getByLabel('交易渠道', { exact: true }).selectOption('证券账户');
  let prompts = 0;
  page.once('dialog', async (dialog) => {
    prompts++;
    await dialog.dismiss();
  });
  await page.evaluate(() => history.back());
  await expect.poll(() => prompts).toBe(1);
  await expect(page).toHaveURL(/tab=conditions/);
  await expect(page.getByRole('dialog').getByLabel('交易渠道', { exact: true })).toHaveValue(
    '证券账户',
  );
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.screenshot({ path: testInfo.outputPath('transaction-modal-390.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  page.once('dialog', async (dialog) => {
    prompts++;
    await dialog.accept();
  });
  await page.getByRole('dialog').getByRole('button', { name: '关闭对话框' }).click();
  await expect(page.getByLabel('已知可用现金（元）')).toHaveValue('2000');
  page.once('dialog', async (dialog) => {
    prompts++;
    await dialog.dismiss();
  });
  await page.getByRole('link', { name: '基金与板块', exact: true }).click();
  await expect(page).toHaveURL(/tab=conditions/);
  await page.getByRole('button', { name: '保存投资条件', exact: true }).click();
  await page.getByRole('link', { name: '基金与板块', exact: true }).click();
  await expect(page).toHaveURL(/\/funds$/);
  expect(prompts).toBe(3);
  expect(warnings.filter((message) => message.includes('only supports one blocker'))).toEqual([]);
});
