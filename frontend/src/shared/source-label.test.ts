import { describe, expect, it } from 'vitest';

import { sourceLabel } from './source-label';

describe('sourceLabel', () => {
  it.each([
    'fund_nav.eastmoney',
    'FUND_NAV.EAST-MONEY',
    'ｆｕｎｄ＿ｎａｖ．ｅａｓｔｍｏｎｅｙ',
    '东方财富.基金净值',
    '东财.基金净值',
    'fund_nav.emdata',
    'fund_nav.push2his',
    'https://datacenter-web.example/data',
  ])('hides every retired provider alias: %s', (source) => {
    expect(sourceLabel(source)).toBe('已退役历史来源（只读、不可用于交易）');
  });

  it('keeps a current source label unchanged', () => {
    expect(sourceLabel('sector_daily.ths')).toBe('sector_daily.ths');
  });
});
