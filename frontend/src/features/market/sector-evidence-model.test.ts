import { describe, expect, it } from 'vitest';
import { evidenceStatus, parseSectorFundamentals } from './sector-evidence-model';
import { evidenceFixture } from './sector-evidence-fixtures';

describe('collected sector evidence contract', () => {
  it('keeps source dates and missing valuation percentile without filling zero', () => {
    const value = parseSectorFundamentals(evidenceFixture());
    expect(value.valuation.as_of).toBe('2026-09-16');
    expect(value.valuation.percentile).toBeNull();
    expect(value.valuation.history_count).toBe(1);
  });
  it('distinguishes failed, incomplete and changed membership rather than marking success', () => {
    expect(evidenceStatus('failed')).toBe('采集失败');
    expect(evidenceStatus('collecting')).toContain('尚未完成');
    expect(evidenceStatus('membership_mismatch')).toContain('成分已变化');
  });
  it('rejects impossible coverage and percentages rather than dropping members', () => {
    const value = evidenceFixture();
    value.operating.periods[0].metrics.profit_yoy.covered = 3;
    expect(() => parseSectorFundamentals(value)).toThrow(/证据格式/);
    const badPercent = evidenceFixture(); badPercent.valuation.percentile = '101';
    expect(() => parseSectorFundamentals(badPercent)).toThrow(/证据格式/);
  });
  it('rejects nonfinite numeric strings and noncanonical dates', () => {
    const value = evidenceFixture(); value.valuation.median_pe_ttm = 'NaN';
    expect(() => parseSectorFundamentals(value)).toThrow();
    value.valuation.median_pe_ttm = '25'; value.valuation.as_of = '2026-02-30';
    expect(() => parseSectorFundamentals(value)).toThrow();
  });
  it('rejects unknown collection status instead of silently displaying available', () => {
    expect(() => parseSectorFundamentals({ ...evidenceFixture(), status: 'success-guessed' })).toThrow();
  });
});
