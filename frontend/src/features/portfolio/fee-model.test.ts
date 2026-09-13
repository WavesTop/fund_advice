import { describe, expect, it } from 'vitest';
import type { FeeRule } from '../../shared/types';
import { validateFeeRule } from './fee-model';

const rule: FeeRule = {
  id: 'r1',
  fundCode: '510300',
  channel: '证券账户',
  source: '示例确认材料',
  effectiveDate: '2026-09-11',
  confirmed: true,
  tiers: [
    { minDays: '0', maxDays: '7', minInclusive: true, maxInclusive: false, rate: '1.5' },
    { minDays: '7', maxDays: '', minInclusive: true, maxInclusive: false, rate: '0' },
  ],
};
describe('费率区间校验', () => {
  it('接受完整且边界没有重叠的期限规则，零费率保留为明确数值', () => {
    expect(validateFeeRule(rule)).toEqual({ errors: {}, gaps: [] });
  });
  it('两条规则同时包含交界日期时阻止保存', () => {
    const result = validateFeeRule({
      ...rule,
      tiers: [{ ...rule.tiers[0], maxInclusive: true }, rule.tiers[1]],
    });
    expect(result.errors['tier-1']).toContain('重叠');
  });
  it('两条规则均排除交界日期时提示具体缺失天数', () => {
    const result = validateFeeRule({
      ...rule,
      confirmed: false,
      tiers: [rule.tiers[0], { ...rule.tiers[1], minInclusive: false }],
    });
    expect(result.gaps).toEqual(['7–7 天未覆盖']);
    expect(result.errors).toEqual({});
    expect(
      validateFeeRule({
        ...rule,
        tiers: [rule.tiers[0], { ...rule.tiers[1], minInclusive: false }],
      }).errors.coverage,
    ).toBeTruthy();
  });
  it('区分未知费率与零，不允许未知费率标为已核对', () => {
    const unknown = { ...rule, tiers: [{ ...rule.tiers[0], rate: '' }, rule.tiers[1]] };
    expect(validateFeeRule(unknown).errors['tier-0']).toContain('费率未知');
    expect(validateFeeRule({ ...unknown, confirmed: false }).errors).toEqual({});
  });
  it('拒绝空区间、非法费率与缺失的确认来源', () => {
    expect(
      validateFeeRule({ ...rule, source: '', tiers: [{ ...rule.tiers[0], minDays: '7' }] }).errors,
    ).toEqual(
      expect.objectContaining({ source: expect.any(String), 'tier-0': expect.any(String) }),
    );
    expect(
      validateFeeRule({ ...rule, tiers: [{ ...rule.tiers[0], rate: '100.001' }, rule.tiers[1]] })
        .errors['tier-0'],
    ).toContain('费率');
  });
  it('无上限区间后再添加规则会重叠，不能被顺序掩盖', () => {
    const result = validateFeeRule({
      ...rule,
      tiers: [rule.tiers[1], rule.tiers[0], { ...rule.tiers[1], minDays: '30' }],
    });
    expect(result.errors['tier-2']).toContain('重叠');
  });
});
