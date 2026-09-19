import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { Badge, EmptyState, Notice, PageHeader, Panel, Stat } from '../../shared/ui';
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
  universe?: { items?: Array<{ code: string; name: string; first_date: string; last_date: string; observation_count: number }> };
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
  };
  action_summary?: Record<string, number>;
  executed_action_summary?: Record<string, number>;
  periods?: Array<{ year: string; start_date: string; end_date: string; return_pct: string; trade_count: number; actions: Record<string, number> }>;
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
};

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
    const curves = (output?.curves ?? []).filter((curve) => ['strategy', 'benchmark'].includes(curve.id));
    const dates = curves[0]?.points.map((point) => point.date) ?? [];
    return {
      animation: false,
      color: ['#315caa', '#7c8b99'],
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
        lineStyle: { width: curve.id === 'strategy' ? 2.4 : 1.7, type: curve.id === 'benchmark' ? 'dashed' : 'solid' },
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
            <div><span className="review-kicker">HISTORICAL REPLAY</span><h2>{output.start_date || record.start_date} 至 {output.end_date || record.end_date}</h2><p>生成于 {formatDateTime(record.created_at)} · {output.input_identity?.common_observations ?? '—'} 个共同观察日 · {output.universe?.items?.length ?? 0} 只已核验基金</p></div>
            <div className="review-personal"><span>策略累计收益</span><strong className={Number(output.metrics?.total_return_pct) >= 0 ? 'positive' : 'negative'}>{formatPercent(output.metrics?.total_return_pct)}</strong><Badge tone={acceptanceStatus === 'passed' ? 'green' : acceptanceStatus === 'review_required' ? 'amber' : acceptanceStatus === 'blocked' ? 'red' : 'neutral'}>{acceptanceStatus === 'passed' ? '验收通过' : acceptanceStatus === 'review_required' ? '需要复盘' : acceptanceStatus === 'blocked' ? '验收阻断' : '需要重新验收'}</Badge></div>
          </div>
          {!output.validation && <Notice tone="warning" title="这份结果缺少独立验收记录">收益曲线仍可查看，但不能据此判断账本恒等式、动作覆盖和分段表现已经通过；请重新运行历史验证。</Notice>}
          {output.status === 'insufficient' && <Notice tone="warning" title="无法完成长期验证">{output.reason || '真实历史资料不足。'}</Notice>}
          {output.metrics && (
            <div className="grid-four">
              <Stat label="期末资产" value={formatMoney(output.metrics.final_equity)} detail={`初始 ${formatMoney(output.metrics.initial_equity)}`} />
              <Stat label="持有基准收益" value={formatPercent(output.metrics.benchmark_return_pct)} detail={`超额 ${formatPercent(output.metrics.excess_return_pct)}`} />
              <Stat label="最大回撤" value={formatPercent(output.metrics.max_drawdown_pct)} detail="按综合策略资产曲线计算" />
              <Stat label="实际模拟成交" value={`${output.metrics.trade_count} 笔`} detail={`累计费用 ${formatMoney(output.metrics.total_fees)}`} />
            </div>
          )}
          {!!output.curves.length && (
            <Panel title="策略整体收益曲线" subtitle="收益率均以同一初始资金归一；基准持有相同基金并使用相同买入费率。">
              <Chart option={chartOption} label="真实历史策略与同基金等权持有基准累计收益率曲线" height={390} />
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
          {!!output.periods?.length && (
            <Panel title="逐年验证" subtitle="每年收益按该年首末共同观察日资产计算，用于发现策略只在局部时期有效的情况。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>年度</th><th>实际区间</th><th>策略收益</th><th>成交数</th><th>动作</th></tr></thead><tbody>{output.periods.map((period) => <tr key={period.year}><td>{period.year}</td><td>{period.start_date} 至 {period.end_date}</td><td className={Number(period.return_pct) >= 0 ? 'positive' : 'negative'}>{formatPercent(period.return_pct)}</td><td>{period.trade_count}</td><td>{Object.entries(period.actions).map(([action, count]) => `${actionNames[action] || action} ${count}`).join(' · ') || '无成交'}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.validation?.performance?.time_segments?.length && (
            <Panel title="前后时段稳定性" subtitle="把完整历史按观察日分为前后两段，检查收益是否只集中在单一市场环境。">
              <div className="table-wrap"><table className="data-table"><thead><tr><th>时段</th><th>策略收益</th><th>基准收益</th><th>超额收益</th></tr></thead><tbody>{output.validation.performance.time_segments.map((segment) => <tr key={segment.segment}><td>{segment.segment === 'earlier' ? '前半段' : '后半段'}</td><td>{formatPercent(segment.strategy.return_pct)}</td><td>{formatPercent(segment.benchmark.return_pct)}</td><td>{formatPercent(segment.excess_return_pct)}</td></tr>)}</tbody></table></div>
            </Panel>
          )}
          {!!output.validation?.performance?.calendar_years?.length && (
            <Panel title="独立逐年复核" subtitle="独立验收器从收益曲线重新计算每年策略、基准、回撤和超额收益。">
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
