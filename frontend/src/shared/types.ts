export type Period = 'short' | 'medium' | 'long';
export type Scenario =
  'ready' | 'empty' | 'loading' | 'offline' | 'error' | 'stale' | 'ai-conflict';
export interface Fund {
  code: string;
  name: string;
  share: string;
  type: string;
  sector: string;
  nav: string;
  change: number;
  coverage: '完整' | '部分资料' | '待补全';
  chart: 'price' | 'nav';
  color: string;
}
export interface Transaction {
  id: string;
  fundCode: string;
  kind: string;
  amount: string;
  shares: string;
  date: string;
  confirmationDate: string;
  settlementDate: string;
  channel: string;
  status: '待补全' | '待确认' | '已确认' | '待核对' | '已撤销';
  fee: string;
  note: string;
  decisionIds: string[];
  createdAt: string;
  corrects?: string;
}
export interface Decision {
  id: string;
  fundCode: string;
  period: Period;
  choice: string;
  amount: string;
  reason: string;
  createdAt: string;
  adviceVersion: string;
  action: string;
}
export interface Conditions {
  cash: string;
  additional: string;
  risk: string;
  concentration: string;
  horizon: string;
  noHoldings: boolean;
  updatedAt: string;
}
export interface DemoTask {
  id: string;
  name: string;
  status: '排队中' | '演示完成' | '失败' | '已取消';
  createdAt: string;
  detail: string;
}
export interface FeeRule {
  id: string;
  fundCode: string;
  channel: string;
  source: string;
  effectiveDate: string;
  confirmed: boolean;
  tiers: {
    minDays: string;
    maxDays: string;
    minInclusive: boolean;
    maxInclusive: boolean;
    rate: string;
  }[];
}
export interface DemoState {
  scenario: Scenario;
  transactions: Transaction[];
  decisions: Decision[];
  conditions: Conditions;
  tasks: DemoTask[];
  strategyVersion: string;
  strategyHistory: { id: string; version: string; at: string; reason: string }[];
  reviewNotes: string;
  feeRules: FeeRule[];
}
