import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { DATA_DATE, displayTime, getFund, money, periods, signed } from '../../shared/data';
import { useDemo, useUnsavedChanges } from '../../shared/store';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Stat, Tabs } from '../../shared/ui';
import { adoptStrategy, reviewSamples, type StrategyVersion } from './model';
import './review.css';

const reviewTabs = [
  { label: '收益对比', value: 'comparison' },
  { label: '决策时间线', value: 'timeline' },
  { label: '策略改良', value: 'strategy' },
];
const lineNames = ['实际记录样例', '短期模拟样例', '中期模拟样例', '长期模拟样例'];
const lineColors = ['#263e51', '#287d91', '#5267c6', '#a27742'];
type Metric = 'asset' | 'profit' | 'rate';
type Comparison = 'aligned' | 'different' | 'missing';

function chartOption(
  metric: Metric,
  comparison: Comparison,
  onlyPeriod?: number,
  visibleLines: boolean[] = [true, true, true, true],
): EChartsOption {
  const indices = onlyPeriod === undefined ? [0, 1, 2, 3] : [0, onlyPeriod + 1];
  const dataFor = (series: number) => {
    const values = metric === 'rate' ? reviewSamples.rate[series] : reviewSamples.profit[series];
    return values.map((value, index) => {
      if (series === 0 && comparison === 'missing') return null;
      if (
        comparison === 'different' &&
        onlyPeriod !== undefined &&
        onlyPeriod > 0 &&
        index < onlyPeriod + 1
      )
        return null;
      // In the differing-period fixture, rebase both lines within each panel.
      const rebased =
        comparison === 'different' && onlyPeriod !== undefined && onlyPeriod > 0
          ? value - values[onlyPeriod + 1]
          : value;
      return metric === 'asset'
        ? rebased + reviewSamples.capital[index]
        : Number(rebased.toFixed(2));
    });
  };
  return {
    color: indices.map((index) => lineColors[index]),
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value) =>
        value == null ? '缺少记录' : `${value}${metric === 'rate' ? '%' : ' 元'}`,
    },
    legend: {
      show: false,
      data: indices.map((index) => lineNames[index]),
      selected: Object.fromEntries(lineNames.map((name, index) => [name, visibleLines[index]])),
    },
    grid: { left: 58, right: 16, top: 30, bottom: 32 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: reviewSamples.dates,
      axisLine: { lineStyle: { color: '#e5eaf0' } },
      axisLabel: { color: '#56687c', fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: metric === 'rate' ? '%' : '元',
      axisLabel: { color: '#56687c', fontSize: 11 },
      splitLine: { lineStyle: { color: '#edf1f5', type: 'dashed' } },
    },
    series: indices.map((index) => ({
      name: lineNames[index],
      type: 'line' as const,
      data: dataFor(index),
      connectNulls: false,
      symbol: index === 0 ? 'circle' : index === 1 ? 'rect' : index === 2 ? 'triangle' : 'diamond',
      symbolSize: 5,
      showSymbol: true,
      lineStyle: {
        width: index === 0 ? 3 : 2,
        type:
          index === 0
            ? ('solid' as const)
            : index === 1
              ? ('dotted' as const)
              : ('dashed' as const),
      },
    })),
  };
}

export function ReviewPage() {
  const { state } = useDemo();
  const [params, setParams] = useSearchParams();
  const tab = reviewTabs.some((item) => item.value === params.get('tab'))
    ? params.get('tab')!
    : 'comparison';
  return (
    <div className="review-page">
      <PageHeader
        eyebrow="REVIEW & REFLECTION"
        title="收益与复盘"
        description="把结果放回当时的决定里，了解差异是怎样发生的。"
        actions={<Badge tone="blue">策略 {state.strategyVersion} · 演示</Badge>}
      />
      <Tabs items={reviewTabs} value={tab} onChange={(value) => setParams({ tab: value })} />
      {tab === 'comparison' && <ComparisonPanel />}
      {tab === 'timeline' && <DecisionTimeline />}
      {tab === 'strategy' && <StrategyPanel />}
    </div>
  );
}

function ComparisonPanel() {
  const { state, notify } = useDemo();
  const [sample, setSample] = useState(true);
  const [metric, setMetric] = useState<Metric>('profit');
  const [comparison, setComparison] = useState<Comparison>('aligned');
  const [mode, setMode] = useState('持续跟踪');
  const [difference, setDifference] = useState<number | null>(null);
  const [visibleLines, setVisibleLines] = useState([true, true, true, true]);
  const visibleSample = sample && state.scenario !== 'empty';
  const comparable = comparison === 'aligned';
  const metricLabel =
    metric === 'asset' ? '资产金额' : metric === 'profit' ? '累计收益金额' : '收益率';
  return (
    <>
      <div className="review-intro">
        <div>
          <span className="review-kicker">观察结果，也保留过程</span>
          <h2>每个周期，独立比较</h2>
          <p>一份实际账本，三个独立模拟账户。差额统一为「模拟 − 实际」。</p>
        </div>
        <div className="review-personal">
          <span>我的实际收益</span>
          <strong>—</strong>
          <span>
            {state.transactions.length
              ? '已保存演示记录，收益待业务模块核算'
              : '尚未建立可核算的实际账本'}
          </span>
        </div>
      </div>
      <div className="toolbar review-controlbar">
        <label className="review-check">
          <input
            type="checkbox"
            checked={sample}
            onChange={(event) => setSample(event.target.checked)}
          />{' '}
          展示虚构比较样例
        </label>
        <label className="review-control">
          评估模式
          <select value={mode} onChange={(event) => setMode(event.target.value)}>
            <option>持续跟踪</option>
            <option>单次建议评估</option>
            <option>事后回测</option>
          </select>
        </label>
        <label className="review-control">
          比较条件
          <select
            value={comparison}
            onChange={(event) => setComparison(event.target.value as Comparison)}
          >
            <option value="aligned">同期间 · 可比样例</option>
            <option value="different">期间不同 · 分别查看</option>
            <option value="missing">实际资料缺失</option>
          </select>
        </label>
      </div>
      {!visibleSample ? (
        <Panel>
          <EmptyState
            title="还没有可核算的收益结果"
            description="演示中保存的交易与决定可以回看。真实资产、收益和模拟结果需接入账本与模拟模块后生成，缺少实际记录不会显示为零收益。"
            action={
              <Link className="button primary" to="/portfolio?tab=transactions">
                查看交易记录
              </Link>
            }
          />
        </Panel>
      ) : (
        <>
          <Notice title="虚构数据 · 仅检查界面展示" tone="info">
            下方「实际记录样例」和三份模拟均为预设示例，不是你的资产、真实行情或投资结论。观察窗口为
            2026-08-12 至 {DATA_DATE}；中长期计划尚未走完完整周期。
          </Notice>
          <Panel
            title={`${mode} · 比较条件`}
            subtitle={
              mode === '事后回测'
                ? '离线补算样例，运行时间 2026-09-13 10:00 北京时间；不代表当时发布过建议。'
                : mode === '单次建议评估'
                  ? '引用示例建议 DEMO-0812，形成于 2026-08-12；只跟踪这一次建议。'
                  : '以各时点保存的示例建议持续接入，三个模拟账户分别使用自己的资金。'
            }
          >
            <div className="review-facts">
              <div>
                <span>比较起点</span>
                <b>{comparison === 'different' ? '按周期分别对齐' : '2026-08-12'}</b>
              </div>
              <div>
                <span>每账户初始资产</span>
                <b>10,000.00 元</b>
              </div>
              <div>
                <span>各账户外部入金</span>
                <b>09-01 · +1,000.00 元</b>
              </div>
              <div>
                <span>费用 / 分红口径</span>
                <b>已扣示例费用 / 区间无分红</b>
              </div>
              <div>
                <span>数据截止</span>
                <b>{DATA_DATE}</b>
              </div>
            </div>
          </Panel>
          <Panel
            title={`${metricLabel}对比 · 虚构样例`}
            subtitle={
              comparison === 'different'
                ? '每个小图分别对齐实际样例与该周期的起点，不绘制跨区间的通用实际基准。'
                : '图例可点击隐藏曲线；资产中的外部入金不计为投资收益。'
            }
            action={
              <div className="review-segment" aria-label="收益图表指标">
                {(
                  [
                    ['asset', '资产金额'],
                    ['profit', '累计收益金额'],
                    ['rate', '收益率'],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    className={metric === value ? 'is-active' : ''}
                    aria-pressed={metric === value}
                    onClick={() => setMetric(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            }
          >
            {comparison === 'different' ? (
              <div className="period-grid">
                {periods.map((period, index) => (
                  <div className="review-mini-chart" key={period.id}>
                    <h3>
                      {period.name} · {['08-12', '08-22', '08-27'][index]} 起点
                    </h3>
                    <Chart
                      option={chartOption(metric, comparison, index, visibleLines)}
                      label={`${period.name}${metricLabel}虚构样例，实际与模拟起点分别对齐`}
                      height={240}
                    />
                  </div>
                ))}
              </div>
            ) : (
              <Chart
                option={chartOption(metric, comparison, undefined, visibleLines)}
                label={`${metricLabel}四曲线虚构示例，实际记录样例及短期、中期、长期模拟样例`}
                height={310}
              />
            )}
            <div className="review-line-controls" role="group" aria-label="曲线显示开关">
              {lineNames.map((name, index) => (
                <label key={name}>
                  <input
                    type="checkbox"
                    checked={visibleLines[index]}
                    onChange={(event) =>
                      setVisibleLines((previous) =>
                        previous.map((visible, position) =>
                          position === index ? event.target.checked : visible,
                        ),
                      )
                    }
                  />
                  <span style={{ color: lineColors[index] }}>{['●', '■', '▲', '◆'][index]}</span>
                  {name}
                </label>
              ))}
            </div>
            <p className="muted review-chart-summary">
              {comparison === 'missing'
                ? '实际曲线缺失；模拟仍可查看，但与实际的差值暂不可比。'
                : comparison === 'different'
                  ? '短期自 08-12、中期自 08-22、长期自 08-27 起算。分期间收益明细等待重新核算，不展示不一致的差值。'
                  : '样例期末累计收益：实际 330.00 元，短期 265.00 元，中期 425.00 元，长期 360.00 元。09-01 入金 1,000 元，当日累计收益未因此增加。'}{' '}
              收益率为预设展示数据，实际计算口径待账本模块提供。
            </p>
          </Panel>
          <div className="period-grid">
            {periods.map((period, index) => (
              <Panel
                key={period.id}
                title={`${period.name}模拟`}
                subtitle={period.range}
                action={
                  <Badge tone={comparable ? 'green' : 'amber'}>
                    {comparable ? '同条件 · 样例可比' : '暂不可比'}
                  </Badge>
                }
              >
                <Stat
                  label="示例净收益"
                  value={
                    comparison === 'different'
                      ? '—'
                      : `${money(reviewSamples.profit[index + 1][6])} 元`
                  }
                  detail={comparison === 'different' ? '分期间结果待重算' : '各账户独立，不可相加'}
                />
                <div className="review-period-detail">
                  <span>实际收益</span>
                  <b>{comparable ? '330.00 元' : '—'}</b>
                  <span>模拟 − 实际</span>
                  <button
                    className={`review-difference ${index === 0 ? 'negative' : 'positive'}`}
                    disabled={!comparable}
                    onClick={() => setDifference(index)}
                  >
                    {comparable ? `${signed([-65, 95, 30][index])} 元 ↗` : '暂不可比'}
                  </button>
                  <span>收益率差</span>
                  <b>{comparable ? `${signed([-0.59, 0.83, 0.28][index])} 个百分点` : '—'}</b>
                </div>
                <p className="muted">
                  {comparison === 'missing'
                    ? '缺少实际账本，无法直接计算差额。'
                    : comparison === 'different'
                      ? '各自期间不同，须取得同口径结果后再比较。'
                      : index === 0
                        ? '实际样例优于短期模拟，负差额完整保留。'
                        : '正差额只描述本样例，不证明策略有效。'}
                </p>
              </Panel>
            ))}
          </div>
          <Panel
            title="比较明细"
            subtitle="金额单位：元。差额 = 模拟 − 实际；收益率之差单位为百分点。"
          >
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>账户</th>
                    <th>观察期间</th>
                    <th>初始 / 外部入金</th>
                    <th>实际收益</th>
                    <th>模拟收益</th>
                    <th>差额</th>
                    <th>实际收益率</th>
                    <th>模拟收益率</th>
                    <th>收益率差（百分点）</th>
                    <th>模拟 / 实际费用</th>
                    <th>风险指标</th>
                  </tr>
                </thead>
                <tbody>
                  {periods.map((period, index) => (
                    <tr key={period.id}>
                      <td>{period.name}</td>
                      <td>
                        2026-
                        {comparison === 'different'
                          ? ['08-12', '08-22', '08-27'][index]
                          : '08-12'}{' '}
                        至 09-11
                      </td>
                      <td>{comparison === 'different' ? '待重新核对' : '10,000.00 / +1,000.00'}</td>
                      <td>{comparable ? '330.00' : '—'}</td>
                      <td>
                        {comparison === 'different'
                          ? '待重算'
                          : money(reviewSamples.profit[index + 1][6])}
                      </td>
                      <td>
                        {comparable ? (
                          <button
                            className="review-text-button"
                            onClick={() => setDifference(index)}
                          >
                            {signed([-65, 95, 30][index])} ↗
                          </button>
                        ) : (
                          '暂不可比'
                        )}
                      </td>
                      <td>{comparable ? '2.99%' : '—'}</td>
                      <td>
                        {comparison === 'different'
                          ? '—'
                          : `${reviewSamples.rate[index + 1][6].toFixed(2)}%`}
                      </td>
                      <td>{comparable ? signed([-0.59, 0.83, 0.28][index]) : '暂不可比'}</td>
                      <td>
                        {comparison === 'different'
                          ? '待重算'
                          : `${[28, 16, 14][index]}.00 / ${comparison === 'missing' ? '—' : '18.00'}`}
                      </td>
                      <td>— · 样本不足</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
      <Panel
        title="建立自己的比较"
        subtitle="起点、账本覆盖与当时建议齐备后，由账本与模拟模块生成。"
        action={
          <button
            className="button secondary"
            onClick={() =>
              notify('比较功能已准备好页面；需后续账本与模拟模块接入，当前不会生成个人收益。')
            }
          >
            查看建立条件
          </button>
        }
      >
        <div className="review-readiness">
          <span>
            实际交易草稿 <b>{state.transactions.length} 笔</b>
          </span>
          <span>
            决定记录 <b>{state.decisions.length} 条</b>
          </span>
          <span>
            收益计算服务 <Badge tone="neutral">待后端接入</Badge>
          </span>
          <Link to="/portfolio?tab=conditions">核对投资条件 →</Link>
        </div>
      </Panel>
      {difference !== null && (
        <Modal
          title={`${periods[difference].name}收益差异 · 样例追溯`}
          onClose={() => setDifference(null)}
          wide
        >
          <Notice tone="info">
            这些编号与执行记录是可追溯布局样例，不属于你的真实交易。实际历史请查看下方个人记录入口。
          </Notice>
          <div className="review-diff-total">
            <span>模拟 − 实际</span>
            <strong className={difference === 0 ? 'negative' : 'positive'}>
              {signed([-65, 95, 30][difference])} 元
            </strong>
          </div>
          <div className="review-timeline">
            <div>
              <small>2026-08-12 · DEMO-ADV-0812</small>
              <b>当时建议：分批观察并等待条件确认</b>
              <p>策略 v1.0，来源为虚构材料；不是后来生成的历史建议。</p>
            </div>
            <div>
              <small>2026-08-13 · DEMO-DEC-0813</small>
              <b>样例决定：部分采纳</b>
              <p>实际样例延后一天投入；三个模拟账户按自己的条件执行。</p>
            </div>
            <div>
              <small>2026-08-14 · DEMO-EXEC-0814</small>
              <b>
                {difference === 0 ? '模拟跳过一笔操作：可用现金不足' : '模拟等待确认后继续执行'}
              </b>
              <p>已保留跳过 / 等待记录。差异可观察到时点与仓位不同，尚未完成精确贡献分解。</p>
            </div>
            <div>
              <small>{DATA_DATE} · DEMO-RESULT-0911</small>
              <b>费用：模拟 {money([28, 16, 14][difference])} 元，实际样例 18.00 元</b>
              <p>当前差额包含已扣示例费用；不能把全部差额归因于费用或单次选择。</p>
            </div>
          </div>
          <div className="toolbar">
            <Link
              className="button secondary"
              to="/review?tab=timeline"
              onClick={() => setDifference(null)}
            >
              查看我的决策时间线
            </Link>
            <button className="button primary" onClick={() => setDifference(null)}>
              完成查看
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function DecisionTimeline() {
  const { state, update, notify } = useDemo();
  const [params, setParams] = useSearchParams();
  const decisionId = params.get('decision');
  const [filter, setFilter] = useState('all');
  const [notes, setNotes] = useState(state.reviewNotes);
  useEffect(() => setNotes(state.reviewNotes), [state.reviewNotes]);
  useUnsavedChanges(notes !== state.reviewNotes);
  const items = [
    ...state.decisions
      .filter((decision) => !decisionId || decision.id === decisionId)
      .map((decision) => ({
        id: decision.id,
        date: decision.createdAt,
        kind: 'decision',
        title: `${getFund(decision.fundCode)?.name ?? decision.fundCode} · ${decision.choice}`,
        detail: decision.reason || '当时未补充理由',
        meta: `${periods.find((period) => period.id === decision.period)?.name} · 建议 ${decision.adviceVersion}`,
        to: `/advice?tab=history&decision=${encodeURIComponent(decision.id)}`,
      })),
    ...state.transactions
      .filter((transaction) => !decisionId || transaction.decisionIds.includes(decisionId))
      .map((transaction) => ({
        id: transaction.id,
        date: transaction.createdAt,
        kind: 'transaction',
        title: `${getFund(transaction.fundCode)?.name ?? (transaction.fundCode || '现金账本')} · ${transaction.kind}`,
        detail: `${transaction.date} 发生；${transaction.note || '未补充备注'}${transaction.corrects ? `；更正原记录 ${transaction.corrects}` : ''}`,
        meta: `${transaction.status} · ${transaction.decisionIds.length ? `关联 ${transaction.decisionIds.length} 个决定` : '自主记录 / 未关联决定'}`,
        to: `/portfolio?tab=transactions&transaction=${encodeURIComponent(transaction.id)}`,
      })),
  ]
    .filter((item) => filter === 'all' || item.kind === filter)
    .sort((a, b) => b.date.localeCompare(a.date));
  return (
    <>
      {decisionId && (
        <Notice title="正在复盘单次决定">
          {state.decisions.some((decision) => decision.id === decisionId)
            ? '仅展示此决定与关联交易，笔记仍保存为本次演示的整体复盘笔记。'
            : '引用的决定当前不可用，没有用新记录替换原引用。'}{' '}
          <button className="review-text-button" onClick={() => setParams({ tab: 'timeline' })}>
            查看全部记录 →
          </button>
        </Notice>
      )}
      <Panel
        title="当时的判断，后来的行动"
        subtitle="仅显示你在当前演示中保存的记录；日期为北京时间。关联用于回看，同一交易不会因多个引用而重复列出。"
        action={
          <select
            aria-label="筛选时间线类型"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="all">全部记录</option>
            <option value="decision">个人决定</option>
            <option value="transaction">交易记录</option>
          </select>
        }
      >
        {!items.length || state.scenario === 'empty' ? (
          <EmptyState
            title="暂时没有决策时间线"
            description="记录个人决定，再关联实际操作；已保存的历史依据与后续更正会在这里串联。"
            action={
              <Link className="button primary" to="/advice">
                查看建议与决策
              </Link>
            }
          />
        ) : (
          <div className="review-timeline">
            {items.map((item) => (
              <div key={`${item.kind}-${item.id}`}>
                <small>{displayTime(item.date)} · 记录时间</small>
                <b>{item.title}</b>
                <p>{item.detail}</p>
                <div className="review-event-footer">
                  <Badge tone={item.kind === 'decision' ? 'blue' : 'neutral'}>{item.meta}</Badge>
                  <Link to={item.to}>查看当时记录 →</Link>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
      <div className="grid-two">
        <Panel title="复盘笔记" subtitle="记录观察和证据，区分已发生事实与仍待验证的原因。">
          <label className="field">
            <span>我的观察</span>
            <textarea
              aria-label="复盘笔记"
              rows={7}
              value={notes}
              maxLength={10000}
              placeholder="例如：操作比计划晚了两天。下次回看时，核对当时是否缺少确认资料……"
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>
          <div className="review-event-footer">
            <span className="muted">{notes.length}/10,000 · 仅保存本机演示草稿</span>
            <button
              className="button primary"
              disabled={notes === state.reviewNotes}
              onClick={() => {
                if (update((previous) => ({ ...previous, reviewNotes: notes })))
                  notify('复盘笔记已保存到本机演示草稿。');
              }}
            >
              保存笔记
            </button>
          </div>
        </Panel>
        <Panel title="问题线索" subtitle="先积累证据，再讨论是否需要修改方法。">
          <div className="review-observation">
            <Badge tone="amber">证据不足</Badge>
            <h3>当前还不能判断策略有效或失效</h3>
            <p>
              已记录 {state.decisions.length} 个决定、{state.transactions.length}{' '}
              笔交易草稿。尚无业务模块核算的结果、独立验证样本与风险指标。
            </p>
            <p className="muted">
              单次亏损、落后或一条解释只是一条观察线索。个人方法尚未表达为规则时，也不能直接进行规则对照。
            </p>
            <Link to="/review?tab=strategy">查看策略改良与验证说明 →</Link>
          </div>
        </Panel>
      </div>
    </>
  );
}

function StrategyPanel() {
  const { state, update, notify } = useDemo();
  const [target, setTarget] = useState<StrategyVersion | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [details, setDetails] = useState(false);
  const requestChange = (version: StrategyVersion) => {
    setTarget(version);
    setAcknowledged(false);
  };
  return (
    <>
      <div className="review-strategy-heading">
        <div>
          <span className="review-kicker">STRATEGY WORKSHOP</span>
          <h2>让每次调整都有依据</h2>
          <p>问题、候选、验证与后续观察，保留成一条完整记录。</p>
        </div>
        <Badge tone="blue">当前演示版本 {state.strategyVersion}</Badge>
      </div>
      <Notice title="候选修订样例 · 有效性未验证" tone="warning">
        以下策略差异与验证布局为虚构样例。启用或恢复仅改变当前演示版本并记录操作，不会运行真实策略或改变历史建议。
      </Notice>
      <div className="grid-two">
        <Panel title="待审视问题" subtitle="对象：系统策略 · 短期 · 观察线索">
          <Badge tone="amber">样例假设</Badge>
          <h3>短周期操作是否过于频繁？</h3>
          <p>
            样例中短期模拟收益低于实际样例，费用更高。需要区分交易频率、时点和仓位的贡献，尚不能判断哪个因素是原因。
          </p>
          <div className="review-evidence">
            <span>支持线索</span>
            <p>样例存在额外费用与一次现金不足跳过记录。</p>
            <span>反对 / 缺失证据</span>
            <p>缺少独立观察期；其他周期样本不足，无法验证是否普遍存在。</p>
          </div>
          <Link to="/review?tab=comparison">回看样例收益差异 →</Link>
        </Panel>
        <Panel title="候选方案 v1.1" subtitle="形成时间：2026-09-13 10:00 北京时间 · UI 示例">
          <div className="review-evidence">
            <span>候选改动</span>
            <p>增加操作前的费用审视与等待确认提示。</p>
            <span>可能取舍</span>
            <p>减少不必要操作，也可能延后机会；收益与风险影响均待验证。</p>
            <span>AI 参与</span>
            <p>未调用 AI；无启用 AI / 仅本地规则的效果对照。</p>
          </div>
          <button className="button secondary" onClick={() => setDetails((value) => !value)}>
            {details ? '收起完整差异' : '查看完整差异'}
          </button>
        </Panel>
      </div>
      {details && (
        <Panel title="v1.0 → v1.1 完整差异 · 样例">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>检查项</th>
                  <th>原方案 v1.0</th>
                  <th>候选 v1.1</th>
                  <th>理由与限制</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>费用审视</td>
                  <td>展示费用依据</td>
                  <td>增加费用与操作理由的复核提示</td>
                  <td>金额阈值尚待业务规则确定</td>
                </tr>
                <tr>
                  <td>等待确认</td>
                  <td>说明确认状态</td>
                  <td>确认资料不足时明确等待条件</td>
                  <td>可能减少操作，也可能延后机会</td>
                </tr>
                <tr>
                  <td>历史记录</td>
                  <td>按原版本保留</td>
                  <td>继续按原版本保留</td>
                  <td>新采用记录只作用于后续演示</td>
                </tr>
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <Panel title="验证与后续观察" subtitle="形成修订的资料、独立验证和后续跟踪分别展示。">
        <div className="period-grid">
          {[
            {
              name: '形成修订的资料',
              state: '样例观察',
              text: '2026-08-12 至 09-11 的虚构比较。收益、费用与风险仅用于展示，不是验证结论。',
            },
            {
              name: '独立验证资料',
              state: '尚未验证',
              text: '没有未参与修订的真实数据；样本重叠、反复试参和 AI 事后知识影响均未评估。',
            },
            {
              name: '采用后跟踪',
              state: '尚无真实结果',
              text: '演示采用历史可查看；后续收益、风险与费用需接入真实跟踪结果后判断。',
            },
          ].map((item) => (
            <div className="review-validation" key={item.name}>
              <Badge tone="neutral">{item.state}</Badge>
              <h3>{item.name}</h3>
              <p>{item.text}</p>
            </div>
          ))}
        </div>
        <p className="muted">
          收益改善、风险恶化、费用变化：均未验证。不能仅凭同一批样本中较好的表现确认候选有效。
        </p>
      </Panel>
      <Panel
        title="版本采用"
        subtitle="每次启用与恢复都需要本人确认。恢复产生新记录，不回写过去的生效历史。"
        action={
          <div className="toolbar">
            <button
              className="button secondary"
              disabled={state.strategyVersion === 'v1.0'}
              onClick={() => requestChange('v1.0')}
            >
              演示恢复 v1.0
            </button>
            <button
              className="button primary"
              disabled={state.strategyVersion === 'v1.1'}
              onClick={() => requestChange('v1.1')}
            >
              演示启用 v1.1
            </button>
          </div>
        }
      >
        {!state.strategyHistory.length ? (
          <p className="muted">当前使用初始演示版本 v1.0，尚无采用操作。</p>
        ) : (
          <div className="review-timeline">
            {[...state.strategyHistory].reverse().map((item) => (
              <div key={item.id}>
                <small>{displayTime(item.at)} · 北京时间</small>
                <b>
                  {item.version === 'v1.0' ? '恢复' : '启用'} {item.version}
                </b>
                <p>{item.reason}</p>
              </div>
            ))}
          </div>
        )}
      </Panel>
      {target && (
        <Modal
          title={`确认演示${target === 'v1.0' ? '恢复' : '启用'} ${target}`}
          onClose={() => setTarget(null)}
        >
          <p>
            当前版本 <b>{state.strategyVersion}</b> → 目标版本 <b>{target}</b>
            。生效时间为本次确认时间，将新增一条采用记录。
          </p>
          <p>
            {target === 'v1.1'
              ? '候选增加费用审视与等待确认提示，尚未验证有效性。'
              : '恢复原版展示规则，保留此前的候选采用与所有历史记录。'}{' '}
            这只改变演示状态；后续真实建议与模拟尚未接入。
          </p>
          <label className="review-check">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />{' '}
            我已查看变更与验证局限，确认本次演示操作
          </label>
          <div className="toolbar review-modal-actions">
            <button className="button secondary" onClick={() => setTarget(null)}>
              保留当前方案
            </button>
            <button
              className="button primary"
              disabled={!acknowledged}
              onClick={() => {
                if (
                  update((previous) =>
                    adoptStrategy(previous, target, new Date().toISOString(), crypto.randomUUID()),
                  )
                ) {
                  notify(
                    `已${target === 'v1.0' ? '恢复' : '启用'}演示版本 ${target}，采用历史已保留。`,
                  );
                  setTarget(null);
                }
              }}
            >
              确认{target === 'v1.0' ? '恢复' : '启用'}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
