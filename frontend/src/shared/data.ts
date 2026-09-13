import type { Fund, Period, DemoState } from './types';

export const DATA_DATE = '2026-09-11';
export const periods: { id: Period; name: string; range: string; color: string }[] = [
  { id: 'short', name: '短期', range: '一周以上 · 一个月以内', color: '#287d91' },
  { id: 'medium', name: '中期', range: '1–3 个月', color: '#5267c6' },
  { id: 'long', name: '长期', range: '3–6 个月', color: '#a27742' },
];
// Deterministic, fictional display fixtures. These are not market quotes or recommendations.
export const funds: Fund[] = [
  {
    code: '510300',
    name: '示例沪深300 ETF',
    share: '场内',
    type: '指数型',
    sector: '宽基指数',
    nav: '4.1820',
    change: 1.26,
    coverage: '完整',
    chart: 'price',
    color: '#3562b3',
  },
  {
    code: '000001',
    name: '示例均衡成长混合',
    share: 'A类',
    type: '混合型',
    sector: '先进制造',
    nav: '1.5268',
    change: 0.82,
    coverage: '完整',
    chart: 'nav',
    color: '#427e72',
  },
  {
    code: '000002',
    name: '示例均衡成长混合',
    share: 'C类',
    type: '混合型',
    sector: '先进制造',
    nav: '1.4892',
    change: 0.79,
    coverage: '完整',
    chart: 'nav',
    color: '#427e72',
  },
  {
    code: '512480',
    name: '示例半导体 ETF',
    share: '场内',
    type: '指数型',
    sector: '半导体',
    nav: '1.2430',
    change: 2.14,
    coverage: '完整',
    chart: 'price',
    color: '#7162a4',
  },
  {
    code: '001100',
    name: '示例红利低波联接',
    share: 'A类',
    type: 'ETF联接',
    sector: '红利低波',
    nav: '1.3675',
    change: 0.36,
    coverage: '部分资料',
    chart: 'nav',
    color: '#ac8048',
  },
  {
    code: '001101',
    name: '示例医药健康混合',
    share: 'A类',
    type: '混合型',
    sector: '医药健康',
    nav: '0.9826',
    change: -0.68,
    coverage: '部分资料',
    chart: 'nav',
    color: '#a26773',
  },
  {
    code: '001102',
    name: '示例清洁能源指数',
    share: 'C类',
    type: '指数型',
    sector: '清洁能源',
    nav: '1.0682',
    change: -1.05,
    coverage: '待补全',
    chart: 'nav',
    color: '#587b8c',
  },
  {
    code: '001103',
    name: '示例消费精选混合',
    share: 'A类',
    type: '混合型',
    sector: '大消费',
    nav: '2.1034',
    change: 0.24,
    coverage: '完整',
    chart: 'nav',
    color: '#917754',
  },
];
export const initialState: DemoState = {
  scenario: 'ready',
  transactions: [],
  decisions: [],
  conditions: {
    cash: '',
    additional: '',
    risk: '',
    concentration: '',
    horizon: '',
    noHoldings: false,
    updatedAt: '',
  },
  tasks: [],
  strategyVersion: 'v1.0',
  strategyHistory: [],
  reviewNotes: '',
  feeRules: [],
};
export const getFund = (code: string) => funds.find((fund) => fund.code === code);
export const money = (value: string | number | null | undefined) =>
  value === '' || value == null
    ? '—'
    : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const signed = (value: number, digits = 2) =>
  `${value > 0 ? '+' : ''}${value.toFixed(digits)}`;
export const displayTime = (value: string) =>
  new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
