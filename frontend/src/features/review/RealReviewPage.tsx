import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { Badge, EmptyState, Notice, PageHeader, Panel, Stat } from '../../shared/ui';
import { sourceLabel } from '../../shared/source-label';
import {
  BackendError,
  apiRequest,
  formatCount,
  formatDateTime,
  formatMoney,
  formatPlainPercent,
  formatPercent,
  jsonRequest,
} from '../../shared/backend';
import './review.css';

type CurvePoint = {
  date: string;
  equity: string;
  cash: string;
  market_value: string;
  return_pct: string;
  drawdown_pct: string;
};
type StrategyCurve = { id: string; name: string; points: CurvePoint[] };
type DataEligibility = {
  source_id?: string;
  registry_decision?: string;
  return_basis?: string;
  eligible_for_signal?: boolean;
  eligible_for_execution?: boolean;
  eligible_for_performance?: boolean;
  signal_blockers?: string[];
  performance_blockers?: string[];
};
type UniverseItem = {
  code: string;
  name: string;
  first_date: string;
  last_date: string;
  observation_count: number;
  series_kind?: string;
  series_source_id?: string;
  catalog_source_id?: string;
  relation_source_id?: string;
  catalog_eligibility?: DataEligibility;
  data_eligibility?: DataEligibility;
  relation_eligibility?: DataEligibility;
};
type Trade = {
  code: string;
  name: string;
  signal_date?: string;
  execution_date: string;
  action: 'BUY' | 'ADD' | 'REDUCE' | 'EXIT';
  amount?: string;
  gross_amount?: string;
  fee?: string;
  units?: string;
  price?: string;
};
type BacktestOutput = {
  version: string;
  status: 'completed' | 'review_required' | 'insufficient';
  reason?: string;
  strategy_version?: string;
  generated_at?: string;
  start_date?: string;
  end_date?: string;
  calendar_basis?: string;
  calendar_document?: Record<string, unknown>;
  protocol?: Record<string, unknown>;
  universe?: { items?: UniverseItem[] };
  investment_eligibility?: {
    status: 'ready' | 'blocked';
    investment_operations_enabled: boolean;
    blockers: string[];
    meaning?: string;
  };
  input_identity?: { common_observations?: number; hash?: string };
  curves: StrategyCurve[];
  metrics?: {
    initial_equity: string;
    final_equity: string;
    total_return_pct: string;
    benchmark_return_pct: string;
    excess_return_pct: string;
    max_drawdown_pct: string;
    total_fees: string;
    trade_count: number;
    trades_per_fund_year: string;
    exposure_matched_return_pct?: string;
    exposure_timing_excess_pct?: string;
  };
  exposure_summary?: {
    observation_count: number;
    average_exposure_pct: string;
    median_exposure_pct: string;
    full_cash_observation_days: number;
    below_10_percent_exposure_days: number;
    invested_observation_days?: number;
    active_exposure_return_observations?: number;
    positive_active_exposure_return_observations?: number;
    positive_active_exposure_return_pct?: string | null;
  };
  risk_quality?: {
    version: string;
    return_observations: number;
    observations_per_year: string;
    risk_free_return_assumption_pct: string;
    minimum_acceptable_return_assumption_pct: string;
    annualized_return_pct: string;
    annualized_volatility_pct: string;
    sharpe_ratio: string | null;
    sortino_ratio: string | null;
    calmar_ratio: string | null;
    max_drawdown_pct: string;
    max_drawdown_duration_observations: number;
    positive_return_observation_pct: string;
    worst_observation_return_pct: string;
  };
  holding_cycles?: {
    trade_count: number;
    completed_cycle_count: number;
    open_cycle_count: number;
    median_holding_observation_days: string | null;
    cycles_lte_10_observation_days: number;
    cycles_lte_20_observation_days: number;
  };
  cost_stress?: Array<{
    buy_fee_bps: string;
    sell_fee_bps: string;
    is_requested_scenario?: boolean;
    final_equity: string;
    total_return_pct: string;
    total_fees: string;
    trade_count: number;
  }>;
  economic_attribution?: {
    cash_return_pct: string;
    market_exposure_return_pct: string;
    signal_and_execution_before_cost_pct: string;
    modeled_transaction_cost_drag_pct: string;
    strategy_total_return_pct: string;
  };
  action_summary?: Record<string, number>;
  executed_action_summary?: Record<string, number>;
  periods?: Array<{ year: string; start_date: string; end_date: string; return_base_date?: string; return_pct: string; trade_count: number; actions: Record<string, number> }>;
  trades: Trade[];
  diagnostics: string[];
  limitations: string[];
  validation?: {
    status: 'passed' | 'review_required' | 'blocked';
    blocking_errors: Array<{ code: string; path?: string; message: string }>;
    review_findings: Array<{ code: string; message: string }>;
    actions?: {
      notional_turnover_pct?: string | null;
      annualized_notional_turnover_pct?: string | null;
      trades_per_fund_year?: string | null;
    };
    performance?: {
      calendar_years?: Array<{ year: string; strategy: { return_pct: string; max_drawdown_pct: string }; benchmark: { return_pct: string; max_drawdown_pct: string }; excess_return_pct: string }>;
      time_segments?: Array<{ segment: string; strategy: { return_pct: string }; benchmark: { return_pct: string }; excess_return_pct: string }>;
    };
  };
};
type BacktestRecord = {
  run_id: string;
  strategy_version: string;
  created_at: string;
  start_date: string;
  end_date: string;
  calendar_basis: string;
  status: string;
  output: BacktestOutput;
};

const actionNames: Record<string, string> = {
  BUY: '买入', ADD: '加仓', HOLD: '持有', REDUCE: '减仓', EXIT: '清仓', WAIT: '等待',
};
const diagnostics: Record<string, string> = {
  strategy_never_trades: '历史回放中从未发生交易，需要检查触发阈值。',
  turnover_review_required: '交易频率偏高，需要继续核对费用和换手约束。',
  invalid_account_balance: '回放出现无效现金或资产余额。',
  eligible_universe_empty: '没有同时通过关系核验与历史数据要求的基金。',
  non_session_disclosures_excluded: '已排除周末披露的净值，避免把披露日误当成可成交日。',
  investment_validation_not_eligible: '数据、执行或样本外验证门槛尚未满足，当前结果不能用于真实资金操作。',
  low_market_exposure: '策略大部分时间的市场敞口较低，收益差异需要和现金及同敞口基准一起判断。',
  underperformed_exposure_matched: '策略低于同敞口无成本基准，择时和交易成本尚未产生可验证的增益。',
  short_holding_period_review: '多数持仓周期较短，需要继续核对申赎确认、费用和可成交性。',
  cost_sensitive: '费用上升会明显侵蚀收益，实际费率和持有期限仍需校准。',
  signal_execution_negative_before_cost: '即使先忽略交易费用，当前信号与执行仍低于同等市场敞口的简单对照。',
};

const blockerNames: Record<string, string> = {
  source_not_registered: '数据来源尚未登记和分级',
  source_not_primary_evidence: '行情来源不是可用于交易的第一手证据',
  source_dataset_mismatch: '来源登记的数据类型与当前行情口径不一致',
  catalog_source_not_registered: '基金身份目录来源尚未登记和分级',
  catalog_source_not_primary_evidence: '基金身份目录不是可用于交易的第一手证据',
  relation_source_not_registered: '基金与跟踪指数的关系来源尚未登记和分级',
  relation_source_not_primary_evidence: '基金与跟踪指数的关系不是第一手证据',
  series_kind_unsupported: '当前序列类型不支持策略判断',
  verified_total_return_missing: '缺少含分红再投资的已核验总收益序列',
  point_in_time_universe_missing: '缺少历史时点基金池，存在事后选择偏差',
  point_in_time_observation_revisions_missing: '缺少历史时点数据版本，无法排除事后修订影响',
  immutable_source_evidence_chain_missing: '目录、行情与跟踪关系尚未绑定不可变原始资料和版本记录',
  stable_investable_universe_policy_missing: '缺少稳定且版本化的可投基金池及成员迁移规则',
  sector_research_signal_not_integrated: '板块估值、经营和成分证据尚未进入交易信号与门槛',
  risk_free_cash_return_series_missing: '缺少同期可投资现金收益序列，当前现金基准按零收益处理',
  shared_live_portfolio_engine_not_used: '历史回放与正式建议尚未共用同一组合资金和结算规则',
  verified_historical_trading_calendar_missing: '尚未用覆盖完整回放区间的官方交易日历核验全部执行日',
  atomic_pretrade_revalidation_missing: '决定转成交前尚未在同一原子事务中重验现金、持仓、约束和行情版本',
  product_specific_execution_terms_missing: '尚未纳入每只基金的申购确认、限购和实际费率',
  walk_forward_holdout_not_completed: '尚未完成滚动样本外验证',
};

const returnBasisNames: Record<string, string> = {
  unadjusted_market_price: '未复权市场价格',
  unit_nav_without_verified_distributions: '单位净值（未核验分红再投资）',
  reinvested_total_return: '含分红再投资总收益',
  unknown: '口径未知',
};

function blockerText(value: string) {
  return blockerNames[value] ?? value;
}

function diagnosisText(value: string) {
  if (diagnostics[value]) return diagnostics[value];
  if (value.startsWith('missing_executed_action:'))
    return `历史回放未覆盖${actionNames[value.split(':')[1]] || value.split(':')[1]}行为。`;
  if (value.startsWith('common_observations:'))
    return `共同历史观察日只有 ${value.split(':')[1]} 个，无法完成长期验证。`;
  return value;
}

function messageOf(cause: unknown) {
  return cause instanceof Error ? cause.message : '历史验证请求失败。';
}

export function RealReviewPage() {
  const [record, setRecord] = useState<BacktestRecord | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [initialCash, setInitialCash] = useState('100000');
  const [buyFee, setBuyFee] = useState('10');
  const [sellFee, setSellFee] = useState('10');

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<{ item: BacktestRecord | null }>('/api/v1/strategy/backtests/latest', {}, controller.signal)
      .then((result) => {
        setRecord(result.item);
        setStatus('ready');
      })
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        if (cause instanceof BackendError && cause.status === 404) {
          setStatus('ready');
          return;
        }
        setError(messageOf(cause));
        setStatus('error');
      });
    return () => controller.abort();
  }, []);

  const runBacktest = async (event: FormEvent) => {
    event.preventDefault();
    setRunning(true);
    setError('');
    try {
      const result = await apiRequest<BacktestRecord>(
        '/api/v1/strategy/backtests',
        jsonRequest({ initial_cash: initialCash, buy_fee_bps: buyFee, sell_fee_bps: sellFee }),
      );
      setRecord(result);
      setStatus('ready');
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setRunning(false);
    }
  };

  const output = record?.output;
  const acceptanceStatus = output?.validation?.status ?? 'not_validated';
  const chartOption = useMemo<EChartsOption>(() => {
    const curveOrder = ['strategy', 'cash', 'benchmark', 'exposure_matched'];
    const curves = (output?.curves ?? [])
      .filter((curve) => curveOrder.includes(curve.id))
      .sort((left, right) => curveOrder.indexOf(left.id) - curveOrder.indexOf(right.id));
    const dates = curves[0]?.points.map((point) => point.date) ?? [];
    return {
      animation: false,
      color: ['#315caa', '#91a0ad', '#7c8b99', '#b17a2d'],
      tooltip: { trigger: 'axis', valueFormatter: (value) => `${Number(value).toFixed(2)}%` },
      legend: { top: 0 },
      grid: { left: 58, right: 24, top: 44, bottom: 46 },
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' }, splitLine: { lineStyle: { color: '#edf1f5', type: 'dashed' } } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 5 }],
      series: curves.map((curve) => ({
        name: curve.name,
        type: 'line',
        showSymbol: false,
        data: curve.points.map((point) => Number(point.return_pct)),
        lineStyle: {
          width: curve.id === 'strategy' ? 2.5 : 1.7,
          type: curve.id === 'strategy' ? 'solid' : curve.id === 'cash' ? 'dotted' : 'dashed',
        },
      })),
    };
  }, [output]);

  return (
    <div className="review-page real-review-page">
      <PageHeader
        eyebrow="RETURN & REVIEW"
        title="收益与复盘"
        description="用已存储的真实基金历史逐日回放同一套策略，并与相同基金的等权持有基准比较。"
      />
      <Panel title="历史验证参数" subtitle="费率单位为基点：10 bp = 0.10%。每次运行都会保存输入、结果和数据身份。">
        <form className="review-run-form" onSubmit={runBacktest}>
          <label className="field"><span>初始资金（元）</span><input type="number" min="1" step="0.01" value={initialCash} onChange={(event) => setInitialCash(event.target.value)} required /></label>
          <label className="field"><span>买入费用（bp）</span><input type="number" min="0" step="0.01" value={buyFee} onChange={(event) => setBuyFee(event.target.value)} required /></label>
          <label className="field"><span>卖出费用（bp）</span><input type="number" min="0" step="0.01" value={sellFee} onChange={(event) => setSellFee(event.target.value)} required /></label>
          <button className="button primary" disabled={running}>{running ? '正在逐日回放…' : record ? '重新运行历史验证' : '运行历史验证'}</button>
        </form>
      </Panel>
      {error && <Notice tone="error">{error}</Notice>}
      {status === 'loading' && <div className="page-loading" role="status">正在读取历史验证结果…</div>}
      {status !== 'loading' && !record && !error && (
        <Panel><EmptyState title="还没有历史验证结果" description="运行后将展示完整收益曲线、基准、六类动作覆盖、逐年结果和异常诊断。" /></Panel>
      )}
      {record && output && (
        <>
          <div className="review-strategy-heading">
            <div><span className="review-kicker">HISTORICAL REPLAY</span><h2>{output.start_date || record.start_date} 至 {output.end_date || record.end_date}</h2><p>生成于 {formatDateTime(record.created_at)} · {output.input_identity?.common_observations ?? '—'} 个共同观察日 · {output.universe?.items?.length ?? 0} 只回放基金</p></div>
            <div className="review-personal"><span>策略累计收益</span><strong className={Number(output.metrics?.total_return_pct) >= 0 ? 'positive' : 'negative'}>{formatPercent(output.metrics?.total_return_pct)}</strong><Badge tone={acceptanceStatus === 'passed' ? 'green' : acceptanceStatus === 'review_required' ? 'amber' : acceptanceStatus === 'blocked' ? 'red' : 'neutral'}>{acceptanceStatus === 'passed' ? '验收通过' : acceptanceStatus === 'review_required' ? '需要复盘' : acceptanceStatus === 'blocked' ? '验收阻断' : '需要重新验收'}</Badge></div>
          </div>
          {!output.validation && <Notice tone="warning" title="这份结果缺少独立验收记录">收益曲线仍可查看，但不能据此判断账本恒等式、动作覆盖和分段表现已经通过；请重新运行历史验证。</Notice>}
          {output.status === 'insufficient' && <Notice tone="warning" title="无法完成长期验证">{output.reason || '真实历史资料不足。'}</Notice>}
          {output.investment_eligibility?.status === 'blocked' && (
            <Notice tone="error" title="当前结果不能用于真实资金操作">
              <p>{output.investment_eligibility.meaning || '以下数据、执行或样本外验证门槛尚未满足。'}</p>
              <ul className="review-diagnostic-list">
                {output.investment_eligibility.blockers.map((item) => <li key={item}>{blockerText(item)}</li>)}
              </ul>
            </Notice>
          )}
          {output.investment_eligibility?.status === 'ready' && (
            <Notice title="投资验证门槛已满足">数据来源、收益口径、历史时点和样本外验证均已通过当前规则检查。</Notice>
          )}
          {output.metrics && (
            <div className="grid-four">
              <Stat label="期末资产" value={formatMoney(output.metrics.final_equity)} detail={`初始 ${formatMoney(output.metrics.initial_equity)}`} />
              <Stat label="持有基准收益" value={formatPercent(output.metrics.benchmark_return_pct)} detail={`策略相差 ${formatPercent(output.metrics.excess_return_pct)}，不等同于阿尔法`} />
              <Stat label="最大回撤" value={formatPercent(output.metrics.max_drawdown_pct)} detail="按综合策略资产曲线计算" />
              <Stat label="实际模拟成交" value={`${output.metrics.trade_count} 笔`} detail={`累计费用 ${formatMoney(output.metrics.total_fees)}`} />
            </div>
          )}
          {output.exposure_summary && (
            <div className="grid-four">
              <Stat label="平均市场敞口" value={formatPercent(output.exposure_summary.average_exposure_pct)} detail="每天有多少资产实际承受市场涨跌" />
              <Stat label="中位市场敞口" value={formatPercent(output.exposure_summary.median_exposure_pct)} detail={`低于 10% 共 ${output.exposure_summary.below_10_percent_exposure_days} 天`} />
              <Stat label="完全空仓" value={`${output.exposure_summary.full_cash_observation_days} 天`} detail={`共 ${output.exposure_summary.observation_count} 个观察日`} />
              <Stat label="完整持仓周期" value={`${output.holding_cycles?.completed_cycle_count ?? 0} 次`} detail={output.holding_cycles?.median_holding_observation_days == null ? '暂无已完成周期' : `中位持有 ${output.holding_cycles.median_holding_observation_days} 个观察日`} />
            </div>
          )}
          {output.risk_quality && (
            <Panel title="风险与收益质量" subtitle={`按相邻真实观察点计算，共 ${output.risk_quality.return_observations} 个收益观察；年化统一假设每年 ${output.risk_quality.observations_per_year} 个观察日。`}>
              <div className="grid-four">
                <Stat label="年化收益" value={formatPercent(output.risk_quality.annualized_return_pct)} detail="由首尾资产及实际收益观察数折算" />
                <Stat label="年化波动" value={formatPlainPercent(output.risk_quality.annualized_volatility_pct)} detail="观察收益总体标准差按 244 日年化" />
                <Stat label="Sharpe" value={formatCount(output.risk_quality.sharpe_ratio)} detail="现金 / 无风险收益假设为 0" />
                <Stat label="Sortino" value={formatCount(output.risk_quality.sortino_ratio)} detail="最低可接受收益为 0，仅惩罚下行" />
              </div>
              <div className="grid-four">
                <Stat label="Calmar" value={formatCount(output.risk_quality.calmar_ratio)} detail="年化收益除以最大回撤绝对值" />
                <Stat label="最长回撤期" value={`${output.risk_quality.max_drawdown_duration_observations} 个观察日`} detail={`连续低于历史高点；最大回撤 ${formatPercent(output.risk_quality.max_drawdown_pct)}`} />
                <Stat label="正收益观察占比" value={formatPlainPercent(output.risk_quality.positive_return_observation_pct)} detail={output.exposure_summary?.positive_active_exposure_return_pct == null ? '按全部相邻观察点统计，不等同择时胜率' : `按全部相邻观察点；有前一日风险敞口的 ${output.exposure_summary.active_exposure_return_observations} 个区间中，正收益占 ${formatPlainPercent(output.exposure_summary.positive_active_exposure_return_pct)}`} />
                <Stat label="最差单观察日" value={formatPercent(output.risk_quality.worst_observation_return_pct)} detail="观察点可能受共同数据日期缺口影响" />
              </div>
            </Panel>
          )}
          {!!output.curves.length && (
            <Panel title="策略与三类基准收益曲线" subtitle="所有曲线使用同一初始资金；必须结合市场敞口和费用判断收益来源。">
              <Chart option={chartOption} label="历史策略、现金、同基金等权持有与同敞口基准累计收益率曲线" height={390} />
              <ul className="review-limitations">
                <li><strong>现金基准：</strong>假设资金不进入市场，用于判断承担风险是否得到回报。</li>
                <li><strong>买入持有基准：</strong>始终持有相同基金，承受接近完整市场风险。</li>
                <li><strong>逐基金同敞口基准：</strong>按每只基金上一观察日的真实持仓权重承接下一期收益、现金收益为零且不计费用，保留基金选择并隔离低仓位影响。</li>
              </ul>
              {output.metrics?.exposure_matched_return_pct != null && <Notice tone={Number(output.metrics.exposure_timing_excess_pct) >= 0 ? 'info' : 'warning'} title="逐基金同敞口比较">逐基金同敞口基准收益 {formatPercent(output.metrics.exposure_matched_return_pct)}；策略相差 {formatPercent(output.metrics.exposure_timing_excess_pct)}。该差值更接近择时、再平衡、执行与费用路径的合并结果，仍不是因果阿尔法或未来收益预测。</Notice>}
            </Panel>
          )}
          <div className="grid-two">
            <Panel title="六类判断覆盖" subtitle="持有和等待不会产生交易，但必须在逐日判断中明确出现。">
              <div className="review-action-counts">{['BUY', 'ADD', 'HOLD', 'REDUCE', 'EXIT', 'WAIT'].map((action) => <div key={action}><span>{actionNames[action]}</span><b>{output.action_summary?.[action] ?? 0}</b><small>{['HOLD', 'WAIT'].includes(action) ? '判断次数' : `成交 ${output.executed_action_summary?.[action] ?? 0} 次`}</small></div>)}</div>
            </Panel>
            <Panel title="自动诊断" subtitle="出现诊断时结果仍保留，但不会被标记为无异常。">
              {output.validation ? <>
                {!output.validation.blocking_errors.length && !output.validation.review_findings.length ? <Notice title="独立验收通过">曲线、账本恒等式、动作覆盖和分段表现检查均未产生待处理项。</Notice> : <ul className="review-diagnostic-list">{output.validation.blocking_errors.map((item) => <li key={`error-${item.code}`}><strong>阻断：</strong>{item.message}{item.path ? `（${item.path}）` : ''}</li>)}{output.validation.review_findings.map((item) => <li key={`review-${item.code}`}>{item.message}</li>)}</ul>}
                <p className="muted">累计名义换手率 {formatPlainPercent(output.validation.actions?.notional_turnover_pct)} · 年化 {formatPlainPercent(output.validation.actions?.annualized_notional_turnover_pct)} · 每基金年均成交 {formatCount(output.validation.actions?.trades_per_fund_year)} 次</p>
              </> : !output.diagnostics.length ? <Notice title="报告自身未列出诊断">仍需重新运行，由独立验收器核对现金、资产、动作覆盖和分段表现。</Notice> : <ul className="review-diagnostic-list">{output.diagnostics.map((item) => <li key={item}>{diagnosisText(item)}</li>)}</ul>}
            </Panel>
          </div>
          {output.holding_cycles && (
            <Panel title="持仓周期检查" subtitle="从每次买入到清仓计算一个完整周期；观察日是共同数据日期，并不等同于自然日。">
              <div className="grid-four">
                <Stat label="完成周期" value={`${output.holding_cycles.completed_cycle_count} 次`} detail={`另有 ${output.holding_cycles.open_cycle_count} 个周期尚未清仓`} />
                <Stat label="中位持有期" value={output.holding_cycles.median_holding_observation_days == null ? '—' : `${output.holding_cycles.median_holding_observation_days} 日`} detail="仅统计已完成周期" />
                <Stat label="10 日内结束" value={`${output.holding_cycles.cycles_lte_10_observation_days} 次`} detail="需重点核对短持有的申赎成本" />
                <Stat label="20 日内结束" value={`${output.holding_cycles.cycles_lte_20_observation_days} 次`} detail={`相关成交共 ${output.holding_cycles.trade_count} 笔`} />
              </div>
            </Panel>
          )}
          {output.economic_attribution && (
            <Panel title="收益来源拆解" subtitle="把策略收益拆成承担市场风险、信号与执行、模拟交易成本三部分；三项应加总为策略收益。">
              <div className="grid-four">
                <Stat label="逐基金同敞口收益" value={formatPercent(output.economic_attribution.market_exposure_return_pct)} detail="保留基金选择与上一观察日真实持仓权重" />
                <Stat label="信号与执行路径差" value={formatPercent(output.economic_attribution.signal_and_execution_before_cost_pct)} detail="零费率策略相对逐基金同敞口对照，含再平衡路径交互" />
                <Stat label="模拟成本拖累" value={formatPercent(output.economic_attribution.modeled_transaction_cost_drag_pct)} detail="本次费率相对零费率策略" />
                <Stat label="策略最终收益" value={formatPercent(output.economic_attribution.strategy_total_return_pct)} detail="以上三项的合计" />
              </div>
            </Panel>
          )}
          {!!output.cost_stress?.length && (
            <Panel title="交易费用压力测试" subtitle="保持信号和成交次数不变，只提高买卖费率，检查账面优势是否会被实际成本吞噬。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>买入 / 卖出费率</th><th>期末资产</th><th>累计收益</th><th>累计费用</th><th>成交数</th></tr></thead><tbody>{output.cost_stress.map((scenario) => <tr key={`${scenario.buy_fee_bps}-${scenario.sell_fee_bps}`}><td>{scenario.buy_fee_bps} / {scenario.sell_fee_bps} bp{scenario.is_requested_scenario ? <small>本次设定</small> : null}</td><td>{formatMoney(scenario.final_equity)}</td><td className={Number(scenario.total_return_pct) >= 0 ? 'positive' : 'negative'}>{formatPercent(scenario.total_return_pct)}</td><td>{formatMoney(scenario.total_fees)}</td><td>{scenario.trade_count}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.universe?.items?.length && (
            <Panel title="基金数据来源与使用资格" subtitle="有历史数值不代表可以用于交易；每只基金分别核对来源、收益口径和允许用途。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>基金</th><th>数据来源</th><th>收益口径</th><th>策略 / 真实操作 / 业绩验证</th></tr></thead><tbody>{output.universe.items.map((item) => {
                const eligibility = item.data_eligibility;
                const catalogEligibility = item.catalog_eligibility;
                const relationEligibility = item.relation_eligibility;
                const sourceId = eligibility?.source_id || item.series_source_id || '未提供';
                const required = [eligibility, relationEligibility, ...(catalogEligibility ? [catalogEligibility] : [])];
                const blockers = [...(catalogEligibility?.performance_blockers ?? []), ...(eligibility?.performance_blockers ?? []), ...(relationEligibility?.performance_blockers ?? [])];
                const signalReady = required.every((value) => value?.eligible_for_signal === true);
                const executionReady = required.every((value) => value?.eligible_for_execution === true);
                const performanceReady = required.every((value) => value?.eligible_for_performance === true);
                return <tr key={item.code}><td>{item.name}<small>{item.code} · {item.observation_count} 条观察</small></td><td>{sourceLabel(sourceId)}<small>基金目录：{sourceLabel(catalogEligibility?.source_id || item.catalog_source_id)}</small><small>跟踪关系：{sourceLabel(relationEligibility?.source_id || item.relation_source_id)}</small><small>{item.first_date} 至 {item.last_date}</small></td><td>{returnBasisNames[eligibility?.return_basis ?? 'unknown'] ?? eligibility?.return_basis ?? '口径未知'}</td><td>{eligibility && relationEligibility ? <><Badge tone={signalReady ? 'green' : 'red'}>{signalReady ? '策略可用' : '策略阻断'}</Badge> <Badge tone={executionReady ? 'green' : 'red'}>{executionReady ? '操作可用' : '操作阻断'}</Badge> <Badge tone={performanceReady ? 'green' : 'amber'}>{performanceReady ? '业绩可验证' : '业绩不可验证'}</Badge>{!!blockers.length && <small>{blockers.map(blockerText).join('；')}</small>}</> : <Badge tone="neutral">待重新运行核验</Badge>}</td></tr>;
              })}</tbody></table></div>
            </Panel>
          )}
          {!!output.periods?.length && (
            <Panel title="逐年验证" subtitle="每年收益以上一年最后观察点为基准；首年以回放首点为基准，避免遗漏跨年后的第一个收益区间。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>年度</th><th>实际区间</th><th>策略收益</th><th>成交数</th><th>动作</th></tr></thead><tbody>{output.periods.map((period) => <tr key={period.year}><td>{period.year}</td><td>{period.start_date} 至 {period.end_date}{period.return_base_date && period.return_base_date !== period.start_date ? <small>收益基准 {period.return_base_date}</small> : null}</td><td className={Number(period.return_pct) >= 0 ? 'positive' : 'negative'}>{formatPercent(period.return_pct)}</td><td>{period.trade_count}</td><td>{Object.entries(period.actions).map(([action, count]) => `${actionNames[action] || action} ${count}`).join(' · ') || '无成交'}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.validation?.performance?.time_segments?.length && (
            <Panel title="前后时段稳定性" subtitle="把完整历史按观察日分为前后两段，检查收益是否只集中在单一市场环境。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>时段</th><th>策略收益</th><th>基准收益</th><th>超额收益</th></tr></thead><tbody>{output.validation.performance.time_segments.map((segment) => <tr key={segment.segment}><td>{segment.segment === 'earlier' ? '前半段' : '后半段'}</td><td>{formatPercent(segment.strategy.return_pct)}</td><td>{formatPercent(segment.benchmark.return_pct)}</td><td>{formatPercent(segment.excess_return_pct)}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.validation?.performance?.calendar_years?.length && (
            <Panel title="独立逐年复核" subtitle="独立验收器以上一年最后观察点为收益基准，重新计算每年策略、基准、回撤和超额收益。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>年度</th><th>策略收益 / 回撤</th><th>基准收益 / 回撤</th><th>超额收益</th></tr></thead><tbody>{output.validation.performance.calendar_years.map((year) => <tr key={year.year}><td>{year.year}</td><td>{formatPercent(year.strategy.return_pct)} / {formatPercent(year.strategy.max_drawdown_pct)}</td><td>{formatPercent(year.benchmark.return_pct)} / {formatPercent(year.benchmark.max_drawdown_pct)}</td><td>{formatPercent(year.excess_return_pct)}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.trades.length && (
            <Panel title="最近的模拟成交" subtitle={`共 ${output.trades.length} 笔；下表展示最近 30 笔，完整数量已计入指标。`}>
              <div className="table-wrap"><table className="data-table"><thead><tr><th>执行日</th><th>基金</th><th>动作</th><th>金额</th><th>费用</th></tr></thead><tbody>{output.trades.slice(-30).reverse().map((trade, index) => <tr key={`${trade.execution_date}-${trade.code}-${index}`}><td>{trade.execution_date}<small>{trade.signal_date ? `信号 ${trade.signal_date}` : '按回放规则执行'}</small></td><td>{trade.name}<small>{trade.code}</small></td><td><Badge tone={trade.action === 'EXIT' ? 'red' : trade.action === 'REDUCE' ? 'amber' : 'blue'}>{actionNames[trade.action]}</Badge></td><td>{formatMoney(trade.gross_amount ?? trade.amount)}</td><td>{formatMoney(trade.fee)}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          <Panel title="验证口径与限制" subtitle={`策略 ${record.strategy_version} · 日历依据 ${record.calendar_basis}`}>
            <ul className="review-limitations">{output.limitations.map((text) => <li key={text}>{text}</li>)}</ul>
          </Panel>
        </>
      )}
    </div>
  );
}
