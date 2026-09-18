import { evidenceStatus, type EvidenceMetric, type SectorFundamentals } from './sector-evidence-model';
import { sourceHref } from '../advice/research-model';
import './sector-evidence.css';

const names = { revenue_yoy: '营业总收入同比', profit_yoy: '归母净利润同比', operating_cashflow_yoy: '经营现金流同比' };
const dateText = (date?: string | null) => date?.slice(0, 10) || '未提供';
function Metric({ metric }: { metric?: EvidenceMetric }) {
  if (!metric) return <>未取得</>;
  return <><strong>{metric.value === null ? metric.covered ? '基期非正，增速不适用' : '未取得' : `${Number(metric.value).toFixed(2)}%`}</strong>
    <small>可比样本 {metric.covered}/{metric.total}{metric.complete ? ' · 样本已齐' : ' · 不代表全板块'}</small>
    {metric.supported_total !== undefined && <small>已接入范围 {metric.supported_total} 只 · 范围外 {metric.unsupported_total ?? 0} 只
      {metric.supported_complete ? ' · 已接入范围样本已齐' : ' · 已接入范围仍有缺项'}</small>}
    <small>最新公告日 {dateText(metric.published_at)}</small>
    {metric.current_sum != null && <small>当期合计 {(Number(metric.current_sum) / 1e8).toFixed(2)} 亿元</small>}
    {!!metric.excluded.length && <details><summary>查看未覆盖原因（{metric.excluded.length}）</summary>
      <ul>{metric.excluded.map((row, i) => <li key={`${row.code}-${i}`}>{row.code}：{row.reason}</li>)}</ul></details>}
  </>;
}
export function SectorEvidencePanel({ evidence, name }: { evidence?: SectorFundamentals; name: string }) {
  if (!evidence) return <p className="notice notice-warning">{name}：当前后端尚未返回新版成分证据，请更新并重启数据服务；不会填入演示数据。</p>;
  const v = evidence.valuation;
  return <section className="sector-evidence-panel" aria-label={`${name}资料采集与覆盖`}>
    <h3>{name} · 资料采集与覆盖</h3>
    <p><strong>{evidenceStatus(evidence.status)}</strong> · 系统观察时间 {evidence.observed_at || '尚无记录'} · 成分快照 {dateText(evidence.membership_as_of)}</p>
    <p className="muted">{evidence.scope}。资料覆盖率不是投资评分；系统观察时间不是公告时间。</p>
    {!!evidence.errors.length && <details open><summary>本次缺口与采集诊断</summary><ul>{evidence.errors.map((error, i) => <li key={i}>{error}</li>)}</ul></details>}
    <h4>经营资料 · {evidenceStatus(evidence.operating.status)}</h4>
    {evidence.operating.periods.length ? <div className="table-wrap"><table className="data-table">
      <caption className="sr-only">{name}成分样本经营数据</caption>
      <thead><tr><th>累计报告期 / 比较基期</th>{Object.values(names).map((title) => <th key={title}>{title}</th>)}</tr></thead>
      <tbody>{evidence.operating.periods.map((period) => <tr key={period.report_date}>
        <th>{period.report_date}<small>对照 {period.base_report_date || '原事实注明的同比基期'}</small></th>
        {Object.keys(names).map((key) => <td key={key}><Metric metric={period.metrics[key]} /></td>)}
      </tr>)}</tbody></table></div> : <p>未取得可比经营样本。重新评估会尝试获取当前成分的报表，而不再只更新行情。</p>}
    <h4>同日估值 · {evidenceStatus(v.status)}</h4>
    <dl className="sector-evidence-values">
      <div><dt>估值业务日期</dt><dd>{dateText(v.as_of)}</dd></div>
      <div><dt>{v.index_pe_ttm !== undefined ? '已核验指数PE' : '正PE成分中位数（非板块PE）'}</dt><dd>{v.index_pe_ttm != null || v.median_pe_ttm != null ? `${Number(v.index_pe_ttm ?? v.median_pe_ttm).toFixed(2)} 倍` : '未取得'}</dd></div>
      {v.total !== undefined && <div><dt>PE资料覆盖 / 正PE / 非正PE</dt><dd>{v.covered ?? 0}/{v.total} · {v.positive_count ?? 0} · {v.nonpositive_count ?? 0}</dd></div>}
      <div><dt>同样本历史观察</dt><dd>{v.history_count} 次 / 跨度 {v.history_span_days} 日</dd></div>
      <div><dt>历史位置（非预测概率）</dt><dd>{v.percentile === null ? '历史不足，暂不判断便宜/昂贵' : `${Number(v.percentile).toFixed(1)}%`}</dd></div>
    </dl>
    {!!v.excluded?.length && <details><summary>估值未覆盖明细（{v.excluded.length}）</summary><ul>{v.excluded.map((row, i) => <li key={`${row.code}-${i}`}>{row.code}：{row.reason}</li>)}</ul></details>}
    <details><summary>来源、行业专属待采集项与计算边界</summary>
      <p>行业专属资料：{evidence.catalysts.required.join('；')}。当前状态：{evidenceStatus(evidence.catalysts.status)}。</p>
      <ul>{evidence.limitations.map((limit, i) => <li key={i}>{limit}</li>)}</ul>
      <ul>{evidence.sources.map((source, i) => { const href = sourceHref(source.url); return href ? <li key={`${source.asset_id}-${i}`}><a href={href} target="_blank" rel="noreferrer">来源响应 {i + 1}</a> · 原始资料 {source.asset_id.slice(0, 12)}</li> : null; })}</ul>
    </details>
  </section>;
}
