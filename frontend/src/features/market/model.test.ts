import { describe, expect, it } from 'vitest';
import {
  filterFunds,
  makeSeries,
  marketFunds,
  resolvePage,
  restrictSeries,
  validateFundCode,
} from './model';

const filters = { query: '', type: '', sector: '', coverage: '', sort: 'code', onlyHeld: false };

describe('基金浏览条件与身份', () => {
  it('同名基金的 A/C 份额保留独立身份，曾用名匹配当前记录', () => {
    expect(
      filterFunds(marketFunds, { ...filters, query: '示例均衡成长' }, new Set()).map(
        (fund) => fund.code,
      ),
    ).toEqual(['000001', '000002']);
    expect(filterFunds(marketFunds, { ...filters, query: '示例稳健成长' }, new Set())[0].code).toBe(
      '000001',
    );
  });
  it('组合筛选不会把未记录持仓当成全库持有', () => {
    expect(filterFunds(marketFunds, { ...filters, onlyHeld: true }, new Set())).toHaveLength(0);
    expect(
      filterFunds(
        marketFunds,
        { ...filters, onlyHeld: true, type: '混合型' },
        new Set(['000001', '510300']),
      ).map((fund) => fund.code),
    ).toEqual(['000001']);
  });
  it('补录拒绝不合法代码，并接受保留前导零的六位代码', () => {
    expect(validateFundCode('000001')).toBe('');
    expect(validateFundCode(' 000001 ')).toBe('');
    for (const code of ['1', '0000012', 'ABC123', '1e0001'])
      expect(validateFundCode(code)).not.toBe('');
  });
  it('非法 URL 页码与筛选缩小后页码不会产生空白页', () => {
    expect(resolvePage('90', 9, 6)).toBe(2);
    expect(resolvePage('-1', 9, 6)).toBe(1);
    expect(resolvePage('1.2', 9, 6)).toBe(1);
    expect(resolvePage('NaN', 0, 6)).toBe(1);
  });
});

describe('图表数据范围', () => {
  it('仅返回选定日期范围，并保留缺失点而不补零', () => {
    const selected = restrictSeries(makeSeries(true), 'custom', '2026-08-16', '2026-08-21');
    expect(selected.length).toBeGreaterThan(0);
    expect(selected.some((point) => point.nav === null)).toBe(true);
    expect(
      selected.every((point) => point.date >= '2026-08-16' && point.date <= '2026-08-21'),
    ).toBe(true);
  });
  it('反向日期区间返回空集，近一月不会改变原始资料', () => {
    const data = makeSeries();
    expect(restrictSeries(data, 'custom', '2026-09-11', '2026-08-01')).toEqual([]);
    expect(restrictSeries(data, 'month', '', '').length).toBeLessThan(data.length);
    expect(data[0].date).toBe('2025-09-11');
  });
});
