import type { Decision, DemoState, Period } from '../../shared/types';
import { isValidDecimal, type FieldErrors } from '../portfolio/model';

export const choices = ['全部采纳', '部分采纳', '未采纳', '暂不操作'] as const;
export const examples: Record<
  Period,
  { sector: string; action: string; reason: string; risk: string; condition: string }
> = {
  short: {
    sector: '宽基指数 · 观察节奏',
    action: '暂不操作',
    reason: '示例呈现：等待短期波动趋稳后再评估，保留现金灵活性。',
    risk: '短期价格波动可能使观察条件失效。',
    condition: '净值历史、可用资金及交易规则核对完成。',
  },
  medium: {
    sector: '先进制造 · 跟踪披露',
    action: '关注并观察',
    reason: '示例呈现：结合基金披露与板块资料，跟踪中期变化。',
    risk: '披露信息有滞后，行业配置不代表当前实时持仓。',
    condition: '持仓范围、风险要求及费用资料完整。',
  },
  long: {
    sector: '红利低波 · 持续评估',
    action: '持有待评估',
    reason: '示例呈现：评估长期配置稳定性，关注费用与集中度。',
    risk: '较长持有期不等于保本，风格和费用均可能影响结果。',
    condition: '投资期限与集中度要求明确，历史资料可用。',
  },
};

export function validateDecision(draft: Omit<Decision, 'id' | 'createdAt'>): FieldErrors {
  const errors: FieldErrors = {};
  if (!choices.includes(draft.choice as (typeof choices)[number]))
    errors.choice = '请选择一种个人决定。';
  if (draft.amount && !isValidDecimal(draft.amount))
    errors.amount = '计划金额须大于 0，最多保留两位小数。';
  if (draft.choice === '部分采纳' && !draft.amount)
    errors.amount = '部分采纳请填写调整后的计划金额。';
  if (draft.choice === '部分采纳' && !draft.reason.trim())
    errors.reason = '请说明部分采纳的调整理由。';
  return errors;
}

export function appendDecision(state: DemoState, decision: Decision): DemoState {
  return { ...state, decisions: [...state.decisions, decision] };
}

export function linkDecision(
  state: DemoState,
  decisionId: string,
  transactionIds: string[],
): DemoState {
  const decision = state.decisions.find((item) => item.id === decisionId);
  if (!decision) return state;
  const replaced = new Set(state.transactions.map((item) => item.corrects).filter(Boolean));
  return {
    ...state,
    transactions: state.transactions.map((item) =>
      transactionIds.includes(item.id) &&
      item.fundCode === decision.fundCode &&
      item.status !== '已撤销' &&
      !replaced.has(item.id)
        ? { ...item, decisionIds: Array.from(new Set([...item.decisionIds, decisionId])) }
        : item,
    ),
  };
}
