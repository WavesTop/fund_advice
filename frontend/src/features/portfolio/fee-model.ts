import type { FeeRule } from '../../shared/types';
import { isValidDecimal, validDate, type FieldErrors } from './model';

export type FeeTier = FeeRule['tiers'][number];
export const emptyTier = (): FeeTier => ({
  minDays: '',
  maxDays: '',
  minInclusive: true,
  maxInclusive: false,
  rate: '',
});

export function validateFeeRule(rule: FeeRule): { errors: FieldErrors; gaps: string[] } {
  const errors: FieldErrors = {};
  const gaps: string[] = [];
  if (!/^\d{6}$/.test(rule.fundCode)) errors.fundCode = '请选择对应基金与份额类别。';
  if (!rule.channel.trim()) errors.channel = '请填写该规则适用的具体渠道。';
  if (rule.effectiveDate && !validDate(rule.effectiveDate))
    errors.effectiveDate = '请填写有效的生效日期。';
  if (rule.confirmed && !rule.source.trim()) errors.source = '标记已核对前，请填写规则来源。';
  if (rule.confirmed && !rule.effectiveDate)
    errors.effectiveDate = '标记已核对前，请填写生效日期。';
  if (!rule.tiers.length) errors.tiers = '请至少添加一个持有期限区间。';
  const ranges: { start: number; end: number; index: number }[] = [];
  rule.tiers.forEach((tier, index) => {
    const key = `tier-${index}`;
    const integer = (value: string) => /^\d+$/.test(value) && Number(value) <= 100000;
    if (!integer(tier.minDays)) {
      errors[key] = '下界必须是 0 到 100000 之间的整数天数。';
      return;
    }
    if (tier.maxDays && !integer(tier.maxDays)) {
      errors[key] = '上界必须是合法整数天数，或留空表示无上限。';
      return;
    }
    const start = Number(tier.minDays) + (tier.minInclusive ? 0 : 1);
    const end = tier.maxDays === '' ? Infinity : Number(tier.maxDays) - (tier.maxInclusive ? 0 : 1);
    if (end < start) {
      errors[key] = '此区间没有任何有效持有天数，请检查上下界及是否包含边界。';
      return;
    }
    if (tier.rate && (!isValidDecimal(tier.rate, 4, true) || Number(tier.rate) > 100))
      errors[key] = '费率须在 0% 到 100% 之间，最多四位小数。';
    if (rule.confirmed && tier.rate === '') errors[key] = '此区间费率未知，暂不能标记为已核对。';
    ranges.push({ start, end, index });
  });
  ranges.sort((left, right) => left.start - right.start);
  let coveredThrough = -1;
  for (const range of ranges) {
    if (range.start <= coveredThrough)
      errors[`tier-${range.index}`] = '这个持有期限与其他区间重叠，请检查边界。';
    else if (range.start > coveredThrough + 1)
      gaps.push(`${coveredThrough + 1}–${range.start - 1} 天未覆盖`);
    coveredThrough = Math.max(coveredThrough, range.end);
  }
  if (ranges.length && coveredThrough !== Infinity)
    gaps.push(`${coveredThrough + 1} 天及以上未覆盖`);
  if (rule.confirmed && gaps.length) errors.coverage = '规则存在期限缺口，可先以未核对状态保存。';
  return { errors, gaps };
}

export function tierLabel(tier: FeeTier): string {
  const lower = `${tier.minInclusive ? '≥' : '>'} ${tier.minDays} 天`;
  return tier.maxDays === ''
    ? `${lower}，无上限`
    : `${lower} 且 ${tier.maxInclusive ? '≤' : '<'} ${tier.maxDays} 天`;
}
