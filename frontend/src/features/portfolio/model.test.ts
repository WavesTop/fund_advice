import { describe, expect, it } from 'vitest';
import { initialState } from '../../shared/data';
import {
  appendTransaction,
  findDuplicate,
  recordStatus,
  validateConditions,
  validateTransaction,
  validDate,
  type TransactionDraft,
} from './model';

const draft: TransactionDraft = {
  fundCode: '510300',
  kind: '买入 / 增仓',
  amount: '1000.00',
  shares: '250.0000',
  date: '2026-09-10',
  confirmationDate: '2026-09-11',
  settlementDate: '',
  channel: '证券账户',
  status: '已确认',
  fee: '0',
  note: '',
  decisionIds: ['d1'],
};
describe('交易记录校验与更正边界', () => {
  it('拒绝溢出精度、负数及不合法日期，而不是自动取整或替换', () => {
    expect(
      validateTransaction({ ...draft, amount: '100.001', shares: '-1', date: '2026-02-30' }),
    ).toEqual(
      expect.objectContaining({
        amount: expect.any(String),
        shares: expect.any(String),
        date: expect.any(String),
      }),
    );
    expect(validDate('2024-02-29')).toBe(true);
    expect(validDate('2026-02-29')).toBe(false);
    expect(
      validateTransaction({ ...draft, confirmationDate: '2026-09-09' }).confirmationDate,
    ).toBeTruthy();
    expect(
      validateTransaction({ ...draft, settlementDate: '2026-09-10' }).settlementDate,
    ).toBeTruthy();
  });
  it('允许保存待补全初始持仓，未知费用不作为零费用', () => {
    const initial = { ...draft, kind: '初始持仓', amount: '', fee: '', confirmationDate: '' };
    expect(validateTransaction(initial)).toEqual({});
    expect(recordStatus(initial)).toBe('待补全');
    expect(recordStatus(draft)).toBe('已确认');
  });
  it('可用份额没有后端校验的卖出只能进入待核对记录', () => {
    expect(recordStatus({ ...draft, kind: '卖出 / 减仓', shares: '9999999' })).toBe('待核对');
    expect(validateTransaction({ ...draft, kind: '卖出 / 减仓', shares: '' }).shares).toBeTruthy();
  });
  it('疑似重复检查识别不同小数格式，排除已撤销与被更正的原记录', () => {
    const state = appendTransaction(
      structuredClone(initialState),
      draft,
      't1',
      '2026-09-11T10:00:00Z',
    );
    expect(
      findDuplicate({ ...draft, amount: '1000', shares: '0250' }, state.transactions)?.id,
    ).toBe('t1');
    const corrected = appendTransaction(
      state,
      { ...draft, corrects: 't1', amount: '1100' },
      't2',
      '2026-09-12T10:00:00Z',
    );
    expect(findDuplicate(draft, corrected.transactions)).toBeUndefined();
  });
  it('更正和撤销仅追加记录，原记录、关联、现金快照均保持', () => {
    const state = appendTransaction(
      {
        ...structuredClone(initialState),
        conditions: { ...initialState.conditions, cash: '5000' },
      },
      draft,
      't1',
      '2026-09-11T10:00:00Z',
    );
    const corrected = appendTransaction(
      state,
      { ...draft, corrects: 't1', amount: '1200', note: '更正确认单金额' },
      't2',
      '2026-09-12T10:00:00Z',
    );
    expect(state.transactions).toHaveLength(1);
    expect(corrected.transactions).toHaveLength(2);
    expect(corrected.transactions[0].amount).toBe('1000.00');
    expect(corrected.transactions[1].decisionIds).toEqual(['d1']);
    expect(corrected.conditions.cash).toBe('5000');
    const canceled = appendTransaction(
      corrected,
      { ...draft, corrects: 't2', status: '已撤销', note: '误录' },
      't3',
      '2026-09-12T11:00:00Z',
    );
    expect(canceled.transactions).toHaveLength(3);
    expect(canceled.transactions[2].status).toBe('已撤销');
  });
  it('未知现金和明确零现金都有效，资金与集中度按不同边界检查', () => {
    expect(validateConditions(initialState.conditions)).toEqual({});
    expect(
      validateConditions({
        ...initialState.conditions,
        cash: '0',
        additional: '0',
        concentration: '100',
      }),
    ).toEqual({});
    expect(
      validateConditions({
        ...initialState.conditions,
        cash: '-1',
        additional: '1.005',
        concentration: '100.01',
      }),
    ).toEqual(
      expect.objectContaining({
        cash: expect.any(String),
        additional: expect.any(String),
        concentration: expect.any(String),
      }),
    );
  });
});
