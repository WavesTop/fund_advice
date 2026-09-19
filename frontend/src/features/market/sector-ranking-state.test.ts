import { describe, expect, it } from 'vitest';
import { sectorSortNotice } from './sector-ranking-state';

const item = (id = 'short', status = 'strong', eligible = true, value: number | null = 5) => ({
  periods: [{ id, status, return_pct: value, strength: { eligible } }],
});

describe('sector sort diagnostics', () => {
  it('does not present unavailable data as a working ranked list', () => {
    expect(sectorSortNotice([item('short', 'insufficient', false)], 'short')).toContain('暂按板块代码展示');
  });
  it('uses the selected horizon, not another period', () => {
    expect(sectorSortNotice([item('short')], 'medium')).toContain('中期：当前筛选结果中没有可排名数据');
    expect(sectorSortNotice([item('short')], 'short')).toContain('1/1 个可比较');
  });
  it('does not count stale or historical-only values', () => {
    expect(sectorSortNotice([item('long', 'stale'), item('long', 'insufficient')], 'long')).toContain('没有可排名数据');
  });
  it('does not count nonfinite and missing returns', () => {
    expect(sectorSortNotice([item('short', 'strong', true, NaN), item('short', 'strong', true, null)], 'short')).toContain('没有可排名数据');
  });
  it('explains partial coverage without changing the input order', () => {
    const rows = [item('short', 'insufficient', false), item('short')];
    const snapshot = JSON.stringify(rows);
    expect(sectorSortNotice(rows, 'short')).toContain('1/2 个可比较');
    expect(JSON.stringify(rows)).toBe(snapshot);
  });
  it('does not display the warning for heat, invalid sort or an empty filter', () => {
    expect(sectorSortNotice([item()], 'heat')).toBeNull();
    expect(sectorSortNotice([item()], 'bad')).toBeNull();
    expect(sectorSortNotice([], 'short')).toBeNull();
  });
});
