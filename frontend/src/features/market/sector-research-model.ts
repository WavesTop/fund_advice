export const researchMethods = [
  'rank_top10',
  'maximin_top10',
  'low_vol_top10',
  'upward_quality',
  'return_top10',
  'all_industries',
] as const;
export type ResearchMethod = (typeof researchMethods)[number];
export type CandidateMethod = Exclude<ResearchMethod, 'all_industries'>;
export type ResearchView = CandidateMethod | 'compare' | 'price_state';
export const methodNames: Record<ResearchMethod, string> = {
  rank_top10: '收益序位试验',
  maximin_top10: '收益与回撤序位折中',
  low_vol_top10: '低波动对照',
  upward_quality: '上行条件筛选',
  return_top10: '历史涨幅对照',
  all_industries: '全行业等权对照',
};
export const methodReasons: Record<CandidateMethod, string> = {
  rank_top10: '模型未来收益相对序位前 10；不保证绝对上涨。',
  maximin_top10: '收益与回撤两个相对分位，取较低者排序前 10；不设绝对风险上限。',
  low_vol_top10: '观察窗口内日波动最小的 10 个行业；不要求趋势向上。',
  upward_quality: '窗口涨幅、近期拟合变化为正，且两项模型输出均高于同组均值；不足 10 个不补齐。',
  return_top10: '只按观察窗口涨幅取前 10，作为原始方法对照。',
};
export const stateNames: Record<string, string> = {
  weakening: '转弱',
  recovery: '修复中',
  down: '下行',
  volatile_up: '高波动上行',
  range: '震荡 / 方向未确认',
  strengthening: '趋势增强',
  steady_up: '稳定上行',
};
export const scenarioNames: Record<string, string> = {
  fee10_default: '默认 · 单边 0.10% 费用',
  fee0_default: '单边 0% 费用',
  fee50_default: '单边 0.50% 费用',
  fee100_default: '单边 1.00% 费用',
  fee10_phase5: '复核起点后移 5 个交易日',
  fee10_phase10: '复核起点后移 10 个交易日',
  fee10_phase15: '复核起点后移 15 个交易日',
  fee10_lag2: '执行延后至 T+2',
  fee10_event_only: '仅复核当日调整，不修复待到账资金',
};
export type ResearchRow = {
  code: string;
  name: string;
  state: string;
  return_pct: number;
  fitted_return_pct: number;
  recent_fitted_return_pct: number;
  recent_sessions: number;
  r2: number;
  up_day_ratio: number;
  max_drawdown_pct: number;
  daily_volatility_pct: number;
  fast_ma_bias_pct: number;
  fast_slow_ma_spread_pct: number;
  return_percentile: number;
  safety_percentile: number;
  curve: number[];
};
type Metrics = {
  start: string;
  end: string;
  total_return_pct: number;
  max_drawdown_pct: number;
  annualized_return_pct: number;
  total_fees: number;
  final_equity: number;
  trade_count: number;
};
type Interval = {
  start_date: string;
  end_date: string;
  total_return_pct: number;
  max_drawdown_pct: number;
};
type Accounts = Record<ResearchMethod, Metrics>;
export type ResearchPeriod = {
  id: string;
  label: string;
  horizon: string;
  window: number;
  observation_start: string;
  as_of: string;
  latest_label_end: string;
  training_dates: number;
  dates: string[];
  rows: ResearchRow[];
  selections: Record<CandidateMethod, string[]>;
  evidence: {
    start: string;
    scenarios: Record<string, Accounts>;
    fresh: Record<string, Accounts>;
    annual: Record<ResearchMethod, { year: string; partial: boolean; value: Interval }[]>;
    episodes: {
      id: string;
      group: string;
      start: string;
      end: string;
      methods: Record<ResearchMethod, { status: string; value: Interval | null }>;
    }[];
  };
};
export type SectorResearchSnapshot = {
  version: string;
  as_of: string;
  data_cutoff: string;
  pool_count: number;
  full_pool_start: string;
  investment_eligible: boolean;
  production_ranking_changed: boolean;
  sources: Record<string, { path: string; sha256: string }>;
  periods: ResearchPeriod[];
};
export function researchView(value: string | null): ResearchView {
  if (value === 'price_state') return value;
  return researchMethods.some((method) => method !== 'all_industries' && method === value)
    ? (value as CandidateMethod)
    : 'compare';
}
export function candidateRows(
  period: ResearchPeriod,
  view: ResearchView,
  query: string,
  priceStateCodes?: string[],
) {
  if (view === 'price_state' && !priceStateCodes)
    throw new Error('Price-state evidence is unavailable');
  const codes =
    view === 'price_state'
      ? [...priceStateCodes!].sort()
      : view === 'compare'
        ? [...new Set(Object.values(period.selections).flat())].sort()
        : period.selections[view];
  const byCode = new Map(period.rows.map((row) => [row.code, row]));
  const needle = query.trim().toLocaleLowerCase();
  return codes
    .map((code) => byCode.get(code)!)
    .filter((row) => `${row.code} ${row.name}`.toLocaleLowerCase().includes(needle));
}
export function pct(value: number) {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}
