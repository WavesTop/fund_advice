import { Link } from 'react-router-dom';
import { Badge } from '../../shared/ui';
import type { SectorOpportunity } from './SectorOpportunities';
import { percent, researchHref, subjectKey } from '../advice/research-model';
import './sector-summary.css';

const periodNames = { short: '短期', medium: '中期', long: '长期' };
const tone = { watch: 'blue', conflict: 'amber', risk: 'red', insufficient: 'neutral' } as const;

export function SectorSummaryTable({ items, from }: { items: SectorOpportunity[]; from: string }) {
  return <div className="table-wrap"><table className="data-table sector-summary-table">
    <caption className="sr-only">板块三周期历史表现与研究证据对照</caption>
    <thead><tr><th scope="col">板块 / 来源</th>{(['short', 'medium', 'long'] as const).map((id) => <th scope="col" key={id}>{periodNames[id]}</th>)}<th scope="col">资料日期 / 详情</th></tr></thead>
    <tbody>{items.map((item) => <tr key={subjectKey(item)}>
      <th scope="row"><Link to={researchHref(item, from)}>{item.name}</Link><small>{item.code} · {item.source_id}</small><small>{item.kind || (item.universe_type === 'hot_board' ? '行业 / 板块' : '参考指数')}</small></th>
      {(['short', 'medium', 'long'] as const).map((id) => {
        const period = item.periods.find((entry) => entry.id === id);
        if (!period) return <td key={id}>该周期资料不足</td>;
        const unavailable = Boolean(item.collection_error) || ['stale', 'insufficient'].includes(period.status);
        return <td key={id}><strong className={!unavailable && period.return_pct !== null ? period.return_pct > 0 ? 'sector-change-up' : period.return_pct < 0 ? 'sector-change-down' : '' : ''}>
          {unavailable ? '—' : percent(period.return_pct)}</strong><small>过去 {period.lookback_sessions} 个交易日 · {period.label}</small>
          <Badge tone={tone[period.opportunity.status]}>{period.opportunity.label}</Badge>
          <small>{!unavailable && period.strength?.eligible && period.strength.rank != null ? `同池排名 ${period.strength.rank}/${period.strength.sample_count}` : '当前不可比较'}</small>
        </td>;
      })}
      <td>{item.as_of || '日期未提供'}{item.collection_error && <small className="sector-summary-warning">行情更新失败</small>}<small><Link to={researchHref(item, from)}>三周期研究与基金匹配 →</Link></small></td>
    </tr>)}</tbody>
  </table></div>;
}
