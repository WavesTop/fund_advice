import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('K-line preview draws real SVG candles on the fund list and switches independent ETF samples', async ({
  page,
}) => {
  await page.goto('/funds');
  const panel = page.locator('.kline-panel');
  await expect(panel.getByRole('heading', { name: 'K 线效果预览 · 模拟行情' })).toBeVisible();
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveAttribute(
    'aria-label',
    /68根日K蜡烛/,
  );
  await expect
    .poll(() => panel.locator('svg path[fill="#bf4752"], svg path[fill="#26806b"]').count())
    .toBeGreaterThan(20);
  await expect(panel.locator('.kline-close')).toContainText('4.1820');
  const first = await panel.locator('svg').innerHTML();
  await panel.getByLabel('K线演示基金').selectOption('512480');
  await expect(panel.locator('.kline-close')).toContainText('1.2430');
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveAttribute(
    'aria-label',
    /示例半导体 ETF/,
  );
  await expect(panel.getByRole('link', { name: '查看这只 ETF 的详情 →' })).toHaveAttribute(
    'href',
    '/funds/512480',
  );
  expect(await panel.locator('svg').innerHTML()).not.toBe(first);
});

test('K-line ranges, moving averages, OHLC tooltip and zoom controls remain interactive', async ({
  page,
}) => {
  await page.goto('/funds/510300');
  const panel = page.locator('.kline-panel');
  await panel.getByRole('button', { name: '近 1 月', exact: true }).click();
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveAttribute(
    'aria-label',
    /24根日K蜡烛/,
  );
  await panel.getByLabel('MA10', { exact: true }).uncheck();
  await expect(panel.getByLabel('MA10', { exact: true })).not.toBeChecked();
  await expect(panel.locator('svg path[stroke="#7852a3"]')).toHaveCount(0);
  await panel.getByRole('img', { name: /虚构开高低收/ }).scrollIntoViewIfNeeded();
  const box = await panel.getByRole('img', { name: /虚构开高低收/ }).boundingBox();
  if (!box) throw new Error('K线图没有可见尺寸');
  await page.mouse.move(box.x + box.width / 2, box.y + 100);
  await expect(panel.locator('svg')).toContainText('模拟日线');
  await expect(panel.locator('svg')).toContainText('成交量');
  const candlePaths = panel.locator('svg path[fill="#bf4752"], svg path[fill="#26806b"]');
  // Hover emphasis changes the hovered candle/bar colors; leave the plot before counting.
  await page.mouse.move(box.x - 10, box.y);
  await expect(candlePaths).toHaveCount(48);
  const fullCount = await candlePaths.count();
  await page.mouse.move(box.x + 60, box.y + 382);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y + 382, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => candlePaths.count()).toBeLessThan(fullCount);
  await panel.getByRole('button', { name: '重置缩放' }).click();
  await expect(candlePaths).toHaveCount(fullCount);
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveAttribute(
    'aria-label',
    /24根日K蜡烛/,
  );
  await panel.getByRole('button', { name: '自定义', exact: true }).click();
  await panel.getByLabel('起始日期', { exact: true }).fill('2026-09-12');
  await expect(panel.getByRole('alert')).toContainText('有效的起止日期');
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveCount(0);
  await panel.getByLabel('起始日期', { exact: true }).fill('2026-09-01');
  await expect(panel.getByRole('img', { name: /虚构开高低收/ })).toHaveAttribute(
    'aria-label',
    /9根日K蜡烛/,
  );
  await panel.getByRole('button', { name: '单位净值', exact: true }).click();
  await expect(page.getByRole('img', { name: /单位净值演示曲线/ })).toBeVisible();
  await page.getByRole('button', { name: '交易价格', exact: true }).click();
  await expect(page.locator('.kline-panel')).toBeVisible();
});

for (const width of [1440, 768, 390]) {
  test(`K-line chart renders without overflow or script errors at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('/funds');
    const panel = page.locator('.kline-panel');
    await expect.poll(() => panel.locator('svg path[fill="#bf4752"]').count()).toBeGreaterThan(10);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
      true,
    );
    await panel.screenshot({ path: testInfo.outputPath(`kline-${width}.png`) });
    const audit = await new AxeBuilder({ page })
      .include('.kline-panel')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(audit.violations).toEqual([]);
    expect(errors).toEqual([]);
  });
}
