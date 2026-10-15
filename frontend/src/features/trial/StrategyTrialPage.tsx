import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Download, FlaskConical } from 'lucide-react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { Notice, PageHeader } from '../../shared/ui';
import frozen from './snapshot.json';
import {
  amount,
  drawdowns,
  groupNames,
  percentage,
  policyNames,
  scenarioNames,
  type Interval,
  type Policy,
  type TrialSnapshot,
} from './model';
import './trial.css';

const snapshot: TrialSnapshot = frozen;
const policies: Policy[] = ['inverse20', 'fixed20'];

function ReturnValue({ value }: { value: number }) {
  return <span className={value < 0 ? 'trial-loss' : 'trial-gain'}>{percentage(value)}</span>;
}

function IntervalCells({ value }: { value: Interval | null }) {
  return value ? (
    <td>
      <ReturnValue value={value.total_return_pct} />
      <small>回撤 {Math.abs(value.max_drawdown_pct).toFixed(2)}%</small>
    </td>
  ) : (
    <td className="muted">历史不足</td>
  );
}

export function StrategyTrialPage() {
  const [params, setParams] = useSearchParams();
  const cohort =
    snapshot.cohorts.find((item) => item.id === params.get('pool')) ?? snapshot.cohorts[3];
  const policy: Policy = params.get('policy') === 'fixed20' ? 'fixed20' : 'inverse20';
  const [view, setView] = useState<'wealth' | 'drawdown'>('wealth');
  const [period, setPeriod] = useState('annual');
  const [group, setGroup] = useState('all');
  const measured = cohort.metrics[policy];
  const rolling = cohort.rolling[policy];
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  };
  const option = useMemo<EChartsOption>(
    () => ({
      animation: false,
      color: ['#237c69', '#b44a68'],
      grid: { left: 62, right: 18, top: 40, bottom: 65 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value) =>
          `${Number(value).toFixed(2)}${view === 'wealth' ? ' 万元' : '%'}`,
      },
      legend: { top: 0, data: policies.map((p) => policyNames[p]) },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: cohort.curve.map((row) => String(row[0])),
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { formatter: view === 'wealth' ? '{value} 万' : '{value}%' },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 0, height: 20 }],
      series: policies.map((p, i) => {
        const values = cohort.curve.map((row) => Number(row[i + 1]));
        return {
          name: policyNames[p],
          type: 'line',
          showSymbol: false,
          lineStyle: { width: p === policy ? 3 : 1.5 },
          data: view === 'wealth' ? values.map((value) => value / 10000) : drawdowns(values),
        };
      }),
    }),
    [cohort, policy, view],
  );
  const rows =
    period === 'annual'
      ? cohort.annual.map((row) => ({
          id: row.year,
          label: `${row.year} 年`,
          policies: row.policies,
        }))
      : cohort.episodes
          .filter(
            (row) =>
              group === 'all' ||
              (group === 'observations' ? row.group.endsWith('observation') : row.group === group),
          )
          .map((row) => ({
            id: row.id,
            label: `${row.requested_start.slice(0, 7)} 至 ${row.requested_end.slice(0, 7)} · ${groupNames[row.group]}`,
            policies: row.policies,
          }));
  const download = () => {
    const blob = new Blob([JSON.stringify({ ...snapshot, cohorts: [cohort] }, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `strategy-trial-${cohort.id}-${snapshot.as_of}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="strategy-trial">
      <PageHeader
        eyebrow="RESEARCH TRIAL · V1"
        title="策略研究试用"
        description={`冻结历史截至 ${snapshot.as_of} · 初始资金 100,000 元 · 未接入实盘`}
        actions={
          <button className="button" onClick={download} title="下载当前基金池的历史研究快照">
            <Download size={16} />
            导出快照
          </button>
        }
      />
      <Notice tone="warning" title="试用资格：历史观察，不是投资认证">
        四组区间累计收益为正，但有亏损阶段；趋势试验尚未稳定胜过分散配置。费用是研究假设，真实产品条款、历史可投基金池和未见样本外验证仍未完成。
      </Notice>

      <div className="trial-controls">
        <label>
          历史基金池
          <select
            aria-label="历史基金池"
            value={cohort.id}
            onChange={(event) => update('pool', event.target.value)}
          >
            {snapshot.cohorts.map((item) => (
              <option value={item.id} key={item.id}>
                {item.label} · {item.start.slice(0, 4)} 起
              </option>
            ))}
          </select>
        </label>
        <fieldset className="trial-segments">
          <legend>观察策略</legend>
          {policies.map((p) => (
            <label key={p}>
              <input
                type="radio"
                name="strategy"
                checked={policy === p}
                onChange={() => update('policy', p)}
              />
              <span>{policyNames[p]}</span>
            </label>
          ))}
        </fieldset>
      </div>
      <p className="trial-caption">
        {cohort.scope} · {cohort.start} 至 {cohort.end} · 不同基金池的累计收益不可直接横比
      </p>

      <dl className="trial-metrics">
        <div>
          <dt>累计模拟净收益</dt>
          <dd>
            <ReturnValue value={measured.total_return_pct} />
          </dd>
        </div>
        <div>
          <dt>年化模拟净收益</dt>
          <dd>{percentage(measured.annualized_return_pct)}</dd>
        </div>
        <div>
          <dt>最大历史回撤</dt>
          <dd>{Math.abs(measured.max_drawdown_pct).toFixed(2)}%</dd>
        </div>
        <div>
          <dt>期末模拟总资产</dt>
          <dd>¥ {amount(measured.final_equity)}</dd>
        </div>
      </dl>

      <section className="trial-section" aria-labelledby="trial-curve-heading">
        <div className="trial-section-heading">
          <h2 id="trial-curve-heading">连续账户曲线</h2>
          <fieldset className="trial-segments compact">
            <legend className="sr-only">曲线模式</legend>
            <label>
              <input
                type="radio"
                name="curve"
                checked={view === 'wealth'}
                onChange={() => setView('wealth')}
              />
              <span>总资产</span>
            </label>
            <label>
              <input
                type="radio"
                name="curve"
                checked={view === 'drawdown'}
                onChange={() => setView('drawdown')}
              />
              <span>回撤</span>
            </label>
          </fieldset>
        </div>
        <Chart
          option={option}
          label={`${cohort.label}两种策略的${view === 'wealth' ? '总资产' : '回撤'}历史曲线`}
          height={340}
        />
        <p className="trial-caption">
          累计模拟费用 ¥ {amount(measured.total_fees)} · {measured.trade_count} 笔成交记录 ·{' '}
          {measured.short_redemption_sales} 笔不足 7
          日赎回。总资产含未到账款项，期末清算不代表全部现金已可用。
        </p>
        <p className="trial-caption">
          连续账户滚动 252 个交易日：{rolling.positive}/{rolling.count} 个窗口正收益，最差{' '}
          {rolling.worst_return_pct === null ? '历史不足' : percentage(rolling.worst_return_pct)}
          。窗口重叠，不是独立投资胜率或新入场回测。
        </p>
      </section>

      <section className="trial-section" aria-labelledby="trial-period-heading">
        <div className="trial-section-heading">
          <h2 id="trial-period-heading">分段表现</h2>
          <fieldset className="trial-segments compact">
            <legend className="sr-only">分段方式</legend>
            <label>
              <input
                type="radio"
                name="period"
                checked={period === 'annual'}
                onChange={() => setPeriod('annual')}
              />
              <span>逐年</span>
            </label>
            <label>
              <input
                type="radio"
                name="period"
                checked={period === 'regime'}
                onChange={() => setPeriod('regime')}
              />
              <span>牛熊区间</span>
            </label>
          </fieldset>
        </div>
        {period === 'regime' && (
          <label className="trial-filter">
            市场阶段
            <select
              aria-label="市场阶段"
              value={group}
              onChange={(event) => setGroup(event.target.value)}
            >
              <option value="all">全部阶段</option>
              <option value="bull">牛市</option>
              <option value="bear">熊市</option>
              <option value="observations">近期观察</option>
            </select>
          </label>
        )}
        <div
          className="trial-table-scroll"
          tabIndex={0}
          role="region"
          aria-label="分段收益与回撤表"
        >
          <table className="trial-table">
            <thead>
              <tr>
                <th scope="col">区间 / 覆盖</th>
                <th scope="col">对照收益 / 回撤</th>
                <th scope="col">趋势收益 / 回撤</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const coverage = row.policies.inverse20;
                return (
                  <tr key={row.id}>
                    <th scope="row">
                      {row.label}
                      <small>
                        {coverage
                          ? `${coverage.start} 至 ${coverage.end}${coverage.partial ? ' · 部分区间' : ''}`
                          : '历史不足，未补零'}
                      </small>
                    </th>
                    {policies.map((p) => (
                      <IntervalCells key={p} value={row.policies[p]} />
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="trial-caption">
          分段延续原持仓和现金，以区间前收盘总资产为起点；回撤仅在该段重置。不是每段重新投入 10
          万元，分段不能相加。牛熊标签仅用于事后评价，不参与交易；近期观察不等同于完整牛熊周期。
        </p>
      </section>

      <section className="trial-section" aria-labelledby="trial-stress-heading">
        <h2 id="trial-stress-heading">费用与执行压力</h2>
        <div className="trial-table-scroll" tabIndex={0} role="region" aria-label="压力测试表">
          <table className="trial-table">
            <thead>
              <tr>
                <th scope="col">同区间情景</th>
                <th scope="col">对照收益 / 回撤</th>
                <th scope="col">趋势收益 / 回撤</th>
              </tr>
            </thead>
            <tbody>
              {cohort.stress.map((row) => (
                <tr key={row.id}>
                  <th scope="row">{scenarioNames[row.id]}</th>
                  {policies.map((p) => (
                    <IntervalCells
                      key={p}
                      value={{
                        ...row.policies[p],
                        start: cohort.start,
                        end: cohort.end,
                        reference_date: null,
                        partial: false,
                      }}
                    />
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="trial-section trial-method" aria-labelledby="trial-method-heading">
        <h2 id="trial-method-heading">
          <FlaskConical size={19} /> 规则与证据边界
        </h2>
        <p>
          <strong>分散配置对照：</strong>股票与黄金合计预算 60%，按过去 60 日波动分配，债券预算
          40%；每 20 个交易日复核，并按过去协方差将预测波动缩至 10% 以内，剩余为现金。10%
          是预测波动目标，不是回撤上限。
        </p>
        <p>
          <strong>趋势加权试验：</strong>在持续配置上，每 5
          个交易日复核符合多周期方向和趋势分条件的股票基金，内部转移最多 10
          个百分点。额外仓位实际形成后计时，20 个交易日退出额外倾斜，冷却 5 日；基础持仓并非每 20
          日全部清仓。其执行节奏也与对照不同，差异不能全归因于趋势排名。
        </p>
        <p>
          <strong>费用与到账假设：</strong>通常每边 0.10%，确认持有不足 7 个自然日的赎回费率下限
          1.50%，不与普通卖出费叠加；普通调仓等待 7 日，确认 T+1、到账
          T+2，现金无利息。以上不是已核验的逐基金申赎条款，也不是场内 ETF 成本。
        </p>
        <p>
          <strong>验证状态：</strong>
          保留现金、成交、费用与历史信号审计；已反复查看的存续基金样本，存在基金池选择偏差。净值首次可知时间、分红权益和到账规则尚未完整核验。历史回测不保证未来收益，试用版不生成下单建议。
        </p>
        <details>
          <summary>冻结来源与基金代码</summary>
          <p>{cohort.codes.join('、')}</p>
          <p>报告：{snapshot.source.path}</p>
          <p>SHA-256：{snapshot.source.sha256}</p>
          <p>报告生成时间：{snapshot.report_generated_at}</p>
        </details>
      </section>
    </div>
  );
}
