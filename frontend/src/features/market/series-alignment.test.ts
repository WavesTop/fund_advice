import { describe, expect, it } from 'vitest';
import { alignRows, commonDates, rangeDates } from './series-alignment';

describe('真实日期联动', () => {
  it('按日期而不是行号对齐，缺少观测保持空白', () => {
    const fund = [
      { date: '2026-09-11', unit_nav: '1' },
      { date: '2026-09-15', unit_nav: '2' },
    ];
    const index = [{ date: '2026-09-11' }, { date: '2026-09-14' }, { date: '2026-09-15' }];
    const dates = commonDates([fund, index]);
    expect(dates).toEqual(['2026-09-11', '2026-09-14', '2026-09-15']);
    expect(alignRows(fund, dates)).toEqual([fund[0], null, fund[1]]);
    expect(dates).not.toContain('2026-09-12');
  });
  it('重复日期不静默选一条', () => {
    expect(() =>
      alignRows([{ date: '2026-09-11' }, { date: '2026-09-11' }], ['2026-09-11']),
    ).toThrow();
  });
  it('使用最后来源日期裁剪区间，不冒用系统今天', () => {
    expect(rangeDates(['2025-01-01', '2025-05-01', '2025-05-15'], 'month')).toEqual([
      '2025-05-01',
      '2025-05-15',
    ]);
  });
  it('空、自定义逆序和越界区间不会生成伪数据', () => {
    expect(rangeDates([], 'all')).toEqual([]);
    expect(rangeDates(['2026-09-11', '2026-09-15'], 'custom', '2026-09-15', '2026-09-11')).toEqual(
      [],
    );
    expect(rangeDates(['2026-09-11', '2026-09-15'], 'custom', '2026-09-10', '2026-09-15')).toEqual(
      [],
    );
  });
});
