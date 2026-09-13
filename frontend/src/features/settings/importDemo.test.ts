import { describe, expect, it } from 'vitest';
import { initialState } from '../../shared/data';
import { adoptStrategy } from '../review/model';
import { createDemoExport, MAX_IMPORT_BYTES, parseDemoImport } from './importDemo';

describe('demo record import boundary', () => {
  it('round-trips records and append-only strategy history', () => {
    const state = adoptStrategy(
      structuredClone(initialState),
      'v1.1',
      '2026-09-13T02:00:00Z',
      'adoption',
    );
    state.reviewNotes = '先核对资料日期，不把观察线索当结论。';
    const exported = createDemoExport(state, '2026-09-13T03:00:00Z');
    expect(parseDemoImport(exported)).toEqual({ state, exportedAt: '2026-09-13T03:00:00Z' });
  });

  it('rejects malformed JSON, another file type, and an unsupported version', () => {
    expect(() => parseDemoImport('{')).toThrow('有效 JSON');
    expect(() => parseDemoImport('{"kind":"sqlite-backup","schemaVersion":1}')).toThrow('仅支持');
    const exportObject = JSON.parse(createDemoExport(initialState));
    exportObject.schemaVersion = 2;
    expect(() => parseDemoImport(JSON.stringify(exportObject))).toThrow('第 1 版');
  });

  it('rejects hidden extra fields rather than retaining unexpected credentials', () => {
    const exportObject = JSON.parse(createDemoExport(initialState));
    exportObject.state.apiKey = 'unexpected-secret';
    expect(() => parseDemoImport(JSON.stringify(exportObject))).toThrow('不支持的字段');
  });

  it('rejects invalid nested record values before replacing existing data', () => {
    const state = structuredClone(initialState);
    state.conditions.cash = '-100';
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('非负十进制数');
    state.conditions.cash = 'Infinity';
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('非负十进制数');
  });

  it('rejects dangling references, invalid dates, and duplicate IDs', () => {
    const state = structuredClone(initialState);
    state.transactions = [
      {
        id: 'trade',
        fundCode: '510300',
        kind: '买入',
        amount: '100',
        shares: '',
        date: '2026-09-12',
        confirmationDate: '',
        settlementDate: '',
        channel: '',
        status: '待确认',
        fee: '',
        note: '',
        decisionIds: ['missing'],
        createdAt: '2026-09-13T01:00:00Z',
      },
    ];
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('关联的决定不存在');
    state.transactions[0].decisionIds = [];
    state.transactions[0].date = '2026-02-30';
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('有效日期');
    state.transactions[0].date = '2026-09-12';
    state.transactions.push({ ...state.transactions[0] });
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('重复编号');
  });

  it('rejects a version that lacks a corresponding adoption event', () => {
    const state = { ...initialState, strategyVersion: 'v1.1' };
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('采用历史不一致');
  });

  it('rejects cyclic or branching correction history', () => {
    const state = structuredClone(initialState);
    const base = {
      id: 'original',
      fundCode: '510300',
      kind: '买入',
      amount: '100',
      shares: '',
      date: '2026-09-12',
      confirmationDate: '',
      settlementDate: '',
      channel: '',
      status: '待确认' as const,
      fee: '',
      note: '',
      decisionIds: [],
      createdAt: '2026-09-13T01:00:00Z',
    };
    state.transactions = [
      { ...base, corrects: 'second' },
      { ...base, id: 'second', corrects: 'original' },
    ];
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('循环');
    state.transactions = [
      base,
      { ...base, id: 'second', corrects: 'original' },
      { ...base, id: 'third', corrects: 'original' },
    ];
    expect(() => parseDemoImport(createDemoExport(state))).toThrow('分叉');
  });

  it('rejects oversized inputs without parsing', () => {
    expect(() => parseDemoImport(' '.repeat(MAX_IMPORT_BYTES + 1))).toThrow('超过 2 MB');
  });

  it('accepts an older demo draft without fee rules and validates new fee tiers', () => {
    const envelope = JSON.parse(createDemoExport(initialState));
    delete envelope.state.feeRules;
    expect(parseDemoImport(JSON.stringify(envelope)).state.feeRules).toEqual([]);
    envelope.state.feeRules = [
      {
        id: 'fee',
        fundCode: '510300',
        channel: '证券账户',
        source: '人工核对',
        effectiveDate: '2026-09-01',
        confirmed: true,
        tiers: [
          { minDays: '0', maxDays: '7', minInclusive: true, maxInclusive: false, rate: '1.5' },
        ],
      },
    ];
    expect(parseDemoImport(JSON.stringify(envelope)).state.feeRules).toHaveLength(1);
    envelope.state.feeRules[0].tiers[0].rate = '101';
    expect(() => parseDemoImport(JSON.stringify(envelope))).toThrow('100%');
  });
});
