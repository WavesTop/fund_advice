import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Badge, EmptyState, Notice, PageHeader, Panel } from '../../shared/ui';
import { useResearchData } from './useResearchData';
import {
  periodIds, periodObservations, percent, researchHref, safeReturnPath,
  selectResearchItems, selectionFrom, sourceHref, subjectKey,
  type FundAssociation, type PeriodId, type ResearchItem, type ResearchPeriod,
} from './research-model';
import { SectorEvidencePanel } from '../market/SectorEvidencePanel';
import { MarketDataRefresh } from '../market/MarketDataRefresh';
import './research.css';

const periodNames = { short: '短期', medium: '中期', long: '长期' };
const evidenceTone = { watch: 'blue', conflict: 'amber', risk: 'red', insufficient: 'neutral' } as const;
const dateText = (value: string | null | undefined) => value?.slice(0, 10) || '未提供';

function EvidenceList({ title, values }: { title: string; values: string[] }) {
  return <section className="research-evidence-list"><h4>{title}</h4>
    {values.length ? <ul>{values.map((text, index) => <li key={`${index}-${text}`}>{text}</li>)}</ul> : <p className="muted">未提供，不视为不存在风险。</p>}
  </section>;
}

function PeriodObservation({ item, period, focused }: { item: ResearchItem; period: ResearchPeriod; focused: boolean }) {
  const state = item.research?.periods.find((entry) => entry.id === period.id);
  const marketUnavailable = Boolean(item.collection_error) || ['stale', 'insufficient'].includes(period.status);
  const boardParams = new URLSearchParams({ q: item.code, scope: item.universe_type === 'hot_board' ? 'hot' : 'tracked', view: 'details' });
  return <article className="research-observation" aria-label={`${item.name} ${period.name}研究`}>
    <div className="research-observation-heading">
      <Link to={researchHref(item)}>{item.name}</Link>
      <Badge tone={evidenceTone[period.opportunity.status]}>{period.opportunity.label}</Badge>
    </div>
    <small className="muted">{item.code} · {item.source_id} · 行情截至 {dateText(item.as_of)}</small>
    <p className="research-conclusion">{period.opportunity.summary}</p>
    <dl className="research-metrics">
      <div><dt>过去 {period.lookback_sessions} 个交易日</dt><dd>{marketUnavailable ? '—' : percent(period.return_pct)}</dd></div>
      <div><dt>窗口最大回撤</dt><dd>{marketUnavailable ? '—' : percent(period.max_drawdown_pct)}</dd></div>
      <div><dt>同池历史排名</dt><dd>{!marketUnavailable && period.strength?.eligible && period.strength.rank != null
        ? `${period.strength.rank} / ${period.strength.sample_count}` : '不可比较'}</dd></div>
    </dl>
    {marketUnavailable && <p className="research-warning">{item.collection_error ? '行情更新失败，旧数值不参与比较。' : period.reason}</p>}
    {!!period.opportunity.challenges.length && <p className="research-warning"><strong>主要反证：</strong>{period.opportunity.challenges[0]}</p>}
    <p className="research-next"><strong>下一观察条件：</strong>{period.opportunity.conditions[0] || '尚未提供可核验条件。'}</p>
    <small className="muted">历史表现不是未来回报预测；正式推荐尚未评估。</small>
    <details className="research-details" open={focused || undefined}>
      <summary>支持、反证与待补资料</summary>
      <EvidenceList title="支持证据" values={period.opportunity.supports} />
      <EvidenceList title="反证与限制" values={period.opportunity.challenges} />
      <EvidenceList title="待确认条件" values={period.opportunity.conditions} />
      <EvidenceList title="资料缺口" values={state?.gaps ?? period.opportunity.missing} />
      <p className="muted">实际观察区间：{dateText(period.observation_start)} 至 {dateText(period.observation_end)}</p>
      <Link to={`/sectors?${boardParams}`}>查看板块原始指标与来源 →</Link>
    </details>
  </article>;
}

function FundMatch({ fund }: { fund: FundAssociation }) {
  const href = sourceHref(fund.evidence_url);
  return <article className="research-fund-match">
    <div><Link to={`/funds/${fund.code}?from=${encodeURIComponent(`/advice?fund=${fund.code}`)}`}>{fund.name}</Link>
      <small>{fund.code} · {fund.status === 'linked' ? '已记录指数关联' : '关联暂不开放'}</small></div>
    <p>核验于 {dateText(fund.verified_at)} · {fund.source_id || '来源未提供'} {href && <a href={href} target="_blank" rel="noreferrer">关系来源</a>}</p>
    <ul>{fund.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
    {fund.status === 'linked' && <Link className="button secondary" to={`/advice?fund=${fund.code}`}>查看该基金的关联研究</Link>}
  </article>;
}

export function ResearchAdvicePage() {
  const [params, setParams] = useSearchParams();
  const selection = selectionFrom(params);
  const [fundInput, setFundInput] = useState(selection.fund);
  const [query, setQuery] = useState('');
  const [visibleCount, setVisibleCount] = useState(20);
  const { result, loading, error, reload, notFound, cancelRead, applyEvaluation } = useResearchData(selection.fund);
  useEffect(() => setFundInput(selection.fund), [selection.fund]);
  useEffect(() => setVisibleCount(20), [query]);
  const data = result?.research;
  const selected = data ? selectResearchItems(data.items, selection) : { items: [], problem: null };
  const focused = Boolean(selection.fund || selection.sector);
  const directoryItems = selected.items.filter((item) => `${item.name} ${item.code}`.toLowerCase().includes(query.trim().toLowerCase()));
  const matchItems = focused ? selected.items : [];
  const back = safeReturnPath(params.get('from'), selection.sector ? '/sectors' : '/funds');
  return <div className="research-page">
    <PageHeader eyebrow="INVESTMENT RESEARCH" title="投资分析与建议"
      description="先核对真实基金与板块，再比较三周期研究线索、反证和基金匹配。"
      actions={result ? <MarketDataRefresh key={selection.fund} target="sectors" onStart={cancelRead} onSettled={(evaluation) => {
        if (evaluation) applyEvaluation(evaluation); else reload();
      }} /> : <button className="button primary" type="button" disabled={loading} onClick={reload}>{loading ? '读取中…' : '重试读取'}</button>} />
    <nav className="research-nav" aria-label="研究页面导航">
      <Link to={back}>返回浏览</Link><Link to="/advice">全部研究范围</Link>
      <Link to="/advice?mode=demo">打开交互演示</Link>
      <Link to="/advice?mode=demo&tab=decisions">演示决策记录</Link>
    </nav>
    <Notice title="真实研究入口 · 正式投资推荐尚未完成">
      此页不使用虚构基金或随机走势。走势观察、投资研究、基金关联分别展示；不会生成买卖指令、目标金额或上涨概率。
      重新评估会采集行业行情、当前成分公司的经营和同日估值，再计算三周期判断；参考指数及旧人工背景不在此批量采集范围，也不调用 AI。
    </Notice>
    <form className="research-search" onSubmit={(event) => {
      event.preventDefault();
      const next = new URLSearchParams();
      if (fundInput.trim()) next.set('fund', fundInput.trim());
      setParams(next);
    }}>
      <label className="field"><span>按真实基金代码查看关联研究</span><input aria-label="研究基金代码" inputMode="numeric" value={fundInput} onChange={(event) => setFundInput(event.target.value)} placeholder="输入六位基金代码" /></label>
      <button className="button secondary" type="submit">查看基金研究</button>
    </form>
    {error && <div className="notice notice-warning" role="alert"><strong>{result ? '本次更新失败，保留上次结果。' : notFound ? '未找到请求的基金。' : '资料暂不可用。'}</strong><p>{error}</p>
      {result && <p>当前结果读取时间：{data?.generated_at}；不代表最新行情。</p>}
      <button className="button secondary" type="button" onClick={reload} disabled={loading}>重试</button>
    </div>}
    {loading && !result && <div className="research-loading" role="status">正在核对真实身份与三周期资料…</div>}
    {result && data && <>
      <Panel title={result.fund ? `${result.fund.name} · ${result.fund.code}` : focused ? selected.items[0]?.name || `请求的板块 ${selection.sector}` : '当前研究范围'}>
        <dl className="research-context"><div><dt>研究方法</dt><dd>{data.method_version}</dd></div>
          <div><dt>本次读取时间</dt><dd>{data.generated_at}</dd></div><div><dt>行情范围</dt><dd>{data.universe?.label || '本地已采集对象'}</dd></div>
          <div><dt>基金推荐 / 个人操作</dt><dd>尚未评估 / 不可用</dd></div></dl>
        {result.fund && <p>份额身份 {result.fund.share_id} · {result.fund.fund_type || '类型未提供'} · 来源 {result.fund.source_id} · <Link to={`/funds/${result.fund.code}`}>查看真实基金走势</Link></p>}
        <p className="muted">读取时间不是资料发布日期。页面展示当前数据投影；重新评估返回的固定研究引用可经研究 API 核对，首次读取不创建快照。</p>
        {data.universe?.last_error && <p className="research-warning">行业池最近更新失败；请以行情及榜单日期核对有效性。</p>}
      </Panel>
      {selected.problem ? <EmptyState title="该对象尚无可用的关联研究" description={selected.problem} action={<Link className="button secondary" to={back}>返回核对对象</Link>} /> : <>
        <div className="research-period-grid">
          {periodIds.map((id: PeriodId) => {
            const observations = periodObservations(data, id, selection);
            const summary = data.advantages?.find((entry) => entry.id === id);
            const horizon = selected.items.flatMap((item) => item.periods).find((period) => period.id === id)?.range;
            const conclusion = observations.length === 1 ? observations[0].periods.find((period) => period.id === id)?.opportunity : undefined;
            return <Panel key={id} className={`research-period research-period-${id}`} title={periodNames[id]} subtitle={horizon ? `未来${horizon}` : '期限资料待提供'} action={<Badge tone={conclusion ? evidenceTone[conclusion.status] : 'neutral'}>{conclusion?.label || (observations.length ? '按对象分别评价' : '该周期暂无资料')}</Badge>}>
              {!focused && <p className="muted">沿用已核验行情的走势观察优先项；不是板块投资推荐榜，也不与参考指数混合排名。</p>}
              {observations.length ? observations.map((item) => {
                const period = item.periods.find((entry) => entry.id === id);
                return period ? <PeriodObservation key={subjectKey(item)} item={item} period={period} focused={focused} /> : <p key={subjectKey(item)}>{item.name}：该周期资料不足。</p>;
              }) : <p className="research-empty">{focused ? '该周期尚无可展示的资料。' : summary?.message || '暂无符合现有观察规则的对象；不勉强给出推荐。'}</p>}
            </Panel>;
          })}
        </div>
        {focused && selected.items.map((item) => <SectorEvidencePanel key={subjectKey(item)} evidence={item.fundamentals} name={item.name} />)}
        <Panel title="对应基金：关联核对与推荐缺口" subtitle="明确关联不等于基金择优，未完成核验不会出现默认候选。">
          {!focused ? <p className="muted">选择一个板块或指数查看其对应基金。下方列表包含已有指数观察，不会把它们与行业走势榜混为同一排名。</p>
            : matchItems.length ? matchItems.map((item) => {
              const associations = (item.research?.fund_associations ?? []).filter((fund) => !selection.fund || fund.code === selection.fund);
              return <section key={subjectKey(item)} className="research-match-group"><h3>{item.name} · {item.code}</h3>
                {associations.length ? associations.map((fund) => <FundMatch key={fund.code} fund={fund} />)
                  : <p className="research-empty">暂无来源与核验时间完整的基金映射。不会按板块名称相似度套用其他基金。</p>}
              </section>;
            }) : <p>暂无可核验的基金关联。</p>}
        </Panel>
        <Panel title={focused ? '当前研究对象' : '全部已采集研究对象'} subtitle="可搜索和进入研究详情；资料不足不等于不看好。">
          <label className="field"><span>搜索板块或指数</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称或代码" /></label>
          <div className="table-wrap"><table className="data-table research-directory"><thead><tr><th>对象 / 来源</th><th>资料日期</th><th>基金关联</th><th>研究</th></tr></thead>
            <tbody>{directoryItems.slice(0, visibleCount).map((item) => <tr key={subjectKey(item)}><td>{item.name}<small>{item.code} · {item.source_id} · {item.universe_type === 'hot_board' ? '行业 / 板块' : '参考指数'}</small></td>
              <td>{dateText(item.as_of)}{item.collection_error && <small>更新失败</small>}</td>
              <td>{item.research?.fund_associations.filter((fund) => fund.status === 'linked').length ?? 0} 只已记录关联<small>未完成基金择优</small></td>
              <td><Link to={researchHref(item)}>查看三周期研究</Link></td></tr>)}</tbody></table></div>
          {!directoryItems.length && <p className="research-empty">没有匹配的研究对象。</p>}
          {directoryItems.length > visibleCount && <button className="button secondary" onClick={() => setVisibleCount((value) => value + 20)}>再显示 20 个对象</button>}
        </Panel>
      </>}
    </>}
  </div>;
}
