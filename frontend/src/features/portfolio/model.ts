import type { Conditions, DemoState, Transaction } from '../../shared/types';

export type TransactionDraft = Omit<Transaction, 'id' | 'createdAt'>;
export type FieldErrors = Record<string, string>;

export const transactionKinds = [
  '买入 / 增仓',
  '卖出 / 减仓',
  '初始持仓',
  '现金入金',
  '现金出金',
  '现金分红',
  '份额变化',
] as const;
export const isCash = (kind: string) => kind === '现金入金' || kind === '现金出金';
export const isSale = (kind: string) => kind === '卖出 / 减仓';
export const isValidDecimal = (value: string, digits = 2, allowZero = false) =>
  new RegExp(`^\\d+(?:\\.\\d{1,${digits}})?$`).test(value) &&
  Number.isFinite(Number(value)) &&
  (allowZero ? Number(value) >= 0 : Number(value) > 0) &&
  Number(value) <= 1_000_000_000_000;

export function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

export function validateTransaction(draft: TransactionDraft): FieldErrors {
  const errors: FieldErrors = {};
  if (!transactionKinds.includes(draft.kind as (typeof transactionKinds)[number]))
    errors.kind = '请选择记录类型。';
  if (!isCash(draft.kind) && !/^\d{6}$/.test(draft.fundCode))
    errors.fundCode = '请选择基金，并核对六位代码与份额类别。';
  if (!draft.channel) errors.channel = '请选择渠道；未知时可选“渠道待补全”。';
  if (!validDate(draft.date)) errors.date = '请填写有效的申请 / 记录日期。';
  if (draft.confirmationDate && !validDate(draft.confirmationDate))
    errors.confirmationDate = '请填写有效的确认日期。';
  if (draft.settlementDate && !validDate(draft.settlementDate))
    errors.settlementDate = '请填写有效的到账日期。';
  if (
    validDate(draft.confirmationDate) &&
    validDate(draft.date) &&
    draft.confirmationDate < draft.date
  )
    errors.confirmationDate = '确认日期不能早于申请日期。';
  if (
    validDate(draft.settlementDate) &&
    validDate(draft.confirmationDate) &&
    draft.settlementDate < draft.confirmationDate
  )
    errors.settlementDate = '到账日期不能早于确认日期。';
  const sharesRequired = isSale(draft.kind) || draft.kind === '份额变化';
  if (sharesRequired && !draft.shares) errors.shares = '请填写本次份额。';
  if (!sharesRequired && !draft.amount && !(draft.kind === '初始持仓' && draft.shares))
    errors.amount = '请填写金额；初始持仓也可先填写份额。';
  if (draft.amount && !isValidDecimal(draft.amount))
    errors.amount = '金额须大于 0，最多保留两位小数。';
  if (draft.shares && !isValidDecimal(draft.shares, 4))
    errors.shares = '份额须大于 0，最多保留四位小数。';
  if (draft.fee && !isValidDecimal(draft.fee, 2, true))
    errors.fee = '费用须为非负金额，最多保留两位小数。';
  return errors;
}

export function recordStatus(draft: TransactionDraft): Transaction['status'] {
  if (draft.status === '已撤销') return '已撤销';
  if (isSale(draft.kind) || draft.status === '待核对') return '待核对';
  if (
    draft.channel === '渠道待补全' ||
    (draft.status === '已确认' &&
      (!draft.confirmationDate || (!isCash(draft.kind) && (!draft.shares || draft.fee === ''))))
  )
    return '待补全';
  return draft.status;
}

export function findDuplicate(
  draft: TransactionDraft,
  transactions: Transaction[],
): Transaction | undefined {
  const replaced = new Set(transactions.map((item) => item.corrects).filter(Boolean));
  const normalize = (value: string) =>
    value === ''
      ? ''
      : value
          .replace(/^0+(?=\d)/, '')
          .replace(/(\.\d*?)0+$/, '$1')
          .replace(/\.$/, '');
  return transactions.find(
    (item) =>
      !replaced.has(item.id) &&
      item.id !== draft.corrects &&
      item.status !== '已撤销' &&
      item.kind === draft.kind &&
      item.fundCode === draft.fundCode &&
      item.date === draft.date &&
      item.channel === draft.channel &&
      normalize(item.amount) === normalize(draft.amount) &&
      normalize(item.shares) === normalize(draft.shares),
  );
}

export function appendTransaction(
  state: DemoState,
  draft: TransactionDraft,
  id: string,
  createdAt: string,
): DemoState {
  return {
    ...state,
    transactions: [...state.transactions, { ...draft, id, createdAt, status: recordStatus(draft) }],
    conditions:
      !isCash(draft.kind) && draft.status !== '已撤销'
        ? { ...state.conditions, noHoldings: false }
        : state.conditions,
  };
}

export function validateConditions(conditions: Conditions): FieldErrors {
  const errors: FieldErrors = {};
  for (const key of ['cash', 'additional'] as const) {
    if (conditions[key] && !isValidDecimal(conditions[key], 2, true))
      errors[key] = '请填写非负金额，最多保留两位小数；未知请留空。';
  }
  if (
    conditions.concentration &&
    (!isValidDecimal(conditions.concentration, 2) || Number(conditions.concentration) > 100)
  )
    errors.concentration = '集中度上限须大于 0 且不超过 100%。';
  return errors;
}
