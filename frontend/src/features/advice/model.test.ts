import { describe, expect, it } from 'vitest';
import { initialState } from '../../shared/data';
import type { Decision, Transaction } from '../../shared/types';
import { appendDecision, linkDecision, validateDecision } from './model';

const decision: Decision = {
  id: 'd1',
  fundCode: '510300',
  period: 'short',
  choice: '全部采纳',
  amount: '',
  reason: '',
  createdAt: '2026-09-11T10:00:00Z',
  adviceVersion: '原始建议 v1',
  action: '暂不操作',
};
const transaction: Transaction = {
  id: 't1',
  fundCode: '510300',
  kind: '买入 / 增仓',
  amount: '1000.00',
  shares: '',
  date: '2026-09-11',
  confirmationDate: '',
  settlementDate: '',
  channel: '证券账户',
  status: '待确认',
  fee: '',
  note: '',
  decisionIds: [],
  createdAt: '2026-09-11T11:00:00Z',
};

describe('个人决定与实际交易独立', () => {
  it('部分采纳必须给出正金额和理由', () => {
    expect(validateDecision({ ...decision, choice: '部分采纳' })).toEqual({
      amount: expect.any(String),
      reason: expect.any(String),
    });
    expect(
      validateDecision({ ...decision, choice: '部分采纳', amount: '500.00', reason: '控制集中度' }),
    ).toEqual({});
    expect(validateDecision({ ...decision, amount: '0' }).amount).toBeTruthy();
  });
  it.each(['全部采纳', '部分采纳', '未采纳', '暂不操作'])(
    '保存%s只新增决定，不创造交易或改变现金',
    (choice) => {
      const state = {
        ...structuredClone(initialState),
        transactions: [transaction],
        conditions: { ...initialState.conditions, cash: '5000' },
      };
      const next = appendDecision(state, { ...decision, choice });
      expect(next.decisions).toHaveLength(1);
      expect(next.transactions).toEqual(state.transactions);
      expect(next.conditions).toEqual(state.conditions);
      expect(state.decisions).toHaveLength(0);
    },
  );
  it('多对多关联不复制交易，重复关联不重复添加标识', () => {
    const state = {
      ...structuredClone(initialState),
      decisions: [decision, { ...decision, id: 'd2', period: 'long' as const }],
      transactions: [transaction, { ...transaction, id: 't2' }],
    };
    const first = linkDecision(state, 'd1', ['t1', 't2']);
    const second = linkDecision(linkDecision(first, 'd2', ['t1']), 'd1', ['t1']);
    expect(second.transactions).toHaveLength(2);
    expect(second.transactions[0].decisionIds).toEqual(['d1', 'd2']);
    expect(second.transactions[1].decisionIds).toEqual(['d1']);
    expect(second.transactions[0].amount).toBe('1000.00');
    expect(state.transactions[0].decisionIds).toEqual([]);
  });
  it('不能关联其他基金、已撤销或者已被更正的交易', () => {
    const state = {
      ...structuredClone(initialState),
      decisions: [decision],
      transactions: [
        transaction,
        { ...transaction, id: 't2', fundCode: '000001' },
        { ...transaction, id: 't3', status: '已撤销' as const },
        { ...transaction, id: 't4', corrects: 't1' },
      ],
    };
    const result = linkDecision(state, 'd1', ['t1', 't2', 't3', 't4']);
    expect(result.transactions.map((item) => item.decisionIds)).toEqual([[], [], [], ['d1']]);
  });
  it('新决定与当前策略变更不覆盖原版本和原选择', () => {
    const state = appendDecision(structuredClone(initialState), decision);
    const next = appendDecision(
      { ...state, strategyVersion: 'v2' },
      { ...decision, id: 'd2', choice: '未采纳', reason: '变更自 d1' },
    );
    expect(next.decisions[0]).toEqual(decision);
    expect(next.decisions[1].choice).toBe('未采纳');
  });
});
