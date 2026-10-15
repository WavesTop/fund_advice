export type Policy = 'inverse20' | 'fixed20';
export type Metrics = {
  total_return_pct: number;
  annualized_return_pct: number;
  max_drawdown_pct: number;
  total_fees: number;
  final_equity: number;
  trade_count: number;
  short_redemption_sales: number;
};
export type Interval = {
  start: string;
  end: string;
  reference_date: string | null;
  partial: boolean;
  total_return_pct: number;
  max_drawdown_pct: number;
};
export type TrialCohort = {
  id: string;
  label: string;
  scope: string;
  codes: string[];
  start: string;
  end: string;
  metrics: Record<Policy, Metrics>;
  curve: (string | number)[][];
  annual: { year: string; policies: Record<Policy, Interval | null> }[];
  episodes: {
    id: string;
    group: string;
    requested_start: string;
    requested_end: string;
    policies: Record<Policy, Interval | null>;
  }[];
  rolling: Record<Policy, { count: number; positive: number; worst_return_pct: number | null }>;
  stress: { id: string; policies: Record<Policy, Metrics> }[];
};
export type TrialSnapshot = {
  version: string;
  as_of: string;
  initial_equity: number;
  report_generated_at: string;
  source: { path: string; sha256: string };
  input_hashes: Record<string, string>;
  cohorts: TrialCohort[];
};

export const policyNames: Record<Policy, string> = {
  inverse20: '分散配置对照',
  fixed20: '趋势加权试验',
};
export const groupNames: Record<string, string> = {
  bull: '牛市',
  bear: '熊市',
  up_observation: '上涨观察',
  down_observation: '下跌观察',
};
export const scenarioNames: Record<string, string> = {
  fee50: '每边费用 0.50%',
  fee100: '每边费用 1.00%',
  phase5: '复核时钟偏移 5 日',
  phase10: '复核时钟偏移 10 日',
  phase15: '复核时钟偏移 15 日',
  lag2: '执行延迟 2 个交易日',
};

export function drawdowns(values: number[]): number[] {
  let peak = values[0];
  return values.map((value) => {
    peak = Math.max(peak, value);
    return (value / peak - 1) * 100;
  });
}

export function percentage(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

export function amount(value: number): string {
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
