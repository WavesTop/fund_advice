import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { initialState } from '../src/shared/data';
import { createDemoExport, parseDemoImport } from '../src/shared/demo-serialization';
import type { DemoState } from '../src/shared/types';

const storageKey = 'fund-advice.demo.v1';
async function stored(page: Page): Promise<DemoState | null> {
  return page.evaluate((key) => JSON.parse(localStorage.getItem(key) || 'null'), storageKey);
}
async function seed(page: Page, state: DemoState) {
  await page.goto('/settings?tab=local');
  await page.evaluate(({ key, state }) => localStorage.setItem(key, JSON.stringify(state)), {
    key: storageKey,
    state,
  });
  await page.reload();
}
function importFile(text: string) {
  return { name: 'fund-advice-demo.json', mimeType: 'application/json', buffer: Buffer.from(text) };
}

test('settings tasks deduplicate, preserve failed attempts and retain cancelled history after reload', async ({
  page,
}) => {
  await page.goto('/settings');
  await page.getByRole('button', { name: '更新全部资料', exact: true }).click();
  await page.getByRole('button', { name: '更新全部资料', exact: true }).click();
  const initial = await stored(page);
  expect(initial?.tasks).toHaveLength(1);
  expect(initial?.tasks[0].status).toBe('排队中');

  await page.getByRole('button', { name: '任务记录', exact: true }).click();
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '演示任务失败' }).click();
  await expect(page.getByRole('dialog')).toContainText('示例来源连接超时');
  await page.getByRole('dialog').getByRole('button', { name: '完成查看' }).click();

  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByRole('button', { name: '重试', exact: true }).click();
  const retried = await stored(page);
  expect(retried?.tasks.map((task) => task.status)).toEqual(['失败', '排队中']);
  expect(retried?.tasks[0].id).toBe(initial?.tasks[0].id);
  expect(retried?.tasks[1].id).not.toBe(initial?.tasks[0].id);

  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('任务记录会保留');
  await page.getByRole('dialog').getByRole('button', { name: '确认取消任务' }).click();
  await page.reload();
  const cancelled = await stored(page);
  expect(cancelled?.tasks.map((task) => task.status)).toEqual(['失败', '已取消']);
  await expect(page.locator('tbody tr')).toHaveCount(2);
  await expect(page.getByText('已移出演示等待队列')).toBeVisible();
  expect(cancelled?.tasks.some((task) => task.status === '演示完成')).toBe(false);
});

test('settings invalid JSON and broken references never replace existing saved records', async ({
  page,
}) => {
  const existing = { ...structuredClone(initialState), reviewNotes: '保留原始复盘笔记' };
  await seed(page, existing);
  const fileInput = page.getByLabel('选择演示记录 JSON 文件');
  await fileInput.setInputFiles(importFile('{not-json'));
  await expect(page.getByRole('alert')).toContainText('文件不是有效 JSON');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await stored(page)).toEqual(existing);

  const incomplete = structuredClone(initialState);
  incomplete.transactions = [
    {
      id: 'trade-a',
      fundCode: '510300',
      kind: '买入 / 增仓',
      amount: '500.00',
      shares: '',
      date: '2026-09-11',
      confirmationDate: '',
      settlementDate: '',
      channel: '证券账户',
      status: '待确认',
      fee: '',
      note: '',
      decisionIds: ['missing-decision'],
      createdAt: '2026-09-13T02:00:00Z',
    },
  ];
  await fileInput.setInputFiles(importFile(createDemoExport(incomplete)));
  await expect(page.getByRole('alert')).toContainText('交易关联的决定不存在');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await stored(page)).toEqual(existing);
});

test('settings valid import requires explicit confirmation and downloaded export can be read back', async ({
  page,
}) => {
  const existing = { ...structuredClone(initialState), reviewNotes: '当前笔记' };
  const imported = {
    ...structuredClone(initialState),
    reviewNotes: '新的复盘笔记',
    conditions: { ...initialState.conditions, cash: '1234.56', updatedAt: '2026-09-13T02:00:00Z' },
  };
  imported.tasks.push({
    id: 'imported-task',
    name: '更新基金目录',
    status: '已取消',
    createdAt: '2026-09-13T02:00:00Z',
    detail: '本人已取消演示请求，无真实执行结果。',
  });
  await seed(page, existing);
  await page
    .getByLabel('选择演示记录 JSON 文件')
    .setInputFiles(importFile(createDemoExport(imported, '2026-09-13T03:00:00Z')));
  const dialog = page.getByRole('dialog', { name: '确认导入演示记录' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认导入', exact: true })).toBeDisabled();
  expect(await stored(page)).toEqual(existing);
  await dialog.getByLabel('我已核对范围，并理解将替换当前演示记录').check();
  await dialog.getByRole('button', { name: '确认导入', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('status')).toContainText('未恢复或改变实际数据库');
  expect(await stored(page)).toEqual(imported);

  await page.reload();
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出演示记录', exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toMatch(/^fund-advice-demo-\d{4}-\d{2}-\d{2}\.json$/);
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  const exported = parseDemoImport(Buffer.concat(chunks).toString('utf8'));
  expect(exported.state).toEqual(imported);
  expect(Number.isFinite(Date.parse(exported.exportedAt))).toBe(true);
});

test('settings AI preview accepts no secret and sends or persists no configuration', async ({
  page,
}) => {
  await page.goto('/settings?tab=ai');
  const requests: string[] = [];
  page.on('request', (request) => requests.push(`${request.method()} ${request.url()}`));
  const before = await stored(page);
  await expect(page.getByLabel('API 密钥')).toBeDisabled();
  await expect(page.getByRole('button', { name: '检查连接 · 待接入' })).toBeDisabled();
  await page.getByLabel('服务类型').selectOption('兼容接口');
  await page.getByLabel('模型名称').fill('example-model');
  await page.getByLabel('接口地址 · 不含密钥').fill('https://example.com/v1');
  await page.getByLabel('个人持仓与交易摘要').check();
  await page.getByRole('button', { name: '预览发送范围' }).click();
  const dialog = page.getByRole('dialog', { name: '资料发送范围 · 仅预览' });
  await expect(dialog.getByText('计划包含，正式启用前需确认')).toBeVisible();
  await expect(dialog.getByText('未发送', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '完成预览' }).click();
  expect(await stored(page)).toEqual(before);
  expect(requests.filter((request) => /example\.com|^POST /.test(request))).toEqual([]);
  await page.reload();
  await expect(page.getByLabel('模型名称')).toHaveValue('');
  await expect(page.getByLabel('API 密钥')).toHaveValue('');
});

test('settings secondary tabs and import runtime dialogs meet automated WCAG A/AA checks', async ({
  page,
}, testInfo) => {
  const violations: { location: string; rule: string; details: unknown }[] = [];
  const audit = async (location: string) => {
    const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    await testInfo.attach(`settings-a11y-${location}`, {
      body: JSON.stringify(result.violations, null, 2),
      contentType: 'application/json',
    });
    for (const violation of result.violations)
      violations.push({
        location,
        rule: violation.id,
        details: violation.nodes.map((node) => ({
          target: node.target,
          reason: node.failureSummary,
        })),
      });
  };
  for (const tab of ['tasks', 'ai', 'local']) {
    await page.goto(`/settings?tab=${tab}`);
    await expect(page.getByRole('heading', { name: '设置', exact: true })).toBeVisible();
    await audit(tab);
  }
  await page.getByRole('button', { name: '查看运行详情' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await audit('runtime-dialog');
  await page.getByRole('dialog').getByRole('button', { name: '完成查看' }).click();
  await page
    .getByLabel('选择演示记录 JSON 文件')
    .setInputFiles(importFile(createDemoExport(initialState)));
  await expect(page.getByRole('dialog', { name: '确认导入演示记录' })).toBeVisible();
  await audit('import-dialog');
  expect(violations).toEqual([]);
});
