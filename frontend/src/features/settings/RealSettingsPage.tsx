import { useCallback, useEffect, useState } from 'react';
import { Database, RefreshCw, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { apiRequest } from '../../shared/backend';
import { Badge, Notice, PageHeader, Panel, Stat } from '../../shared/ui';
import './settings.css';

type Health = {
  status: string;
  api_contract: string;
  capabilities: string[];
  schema_version: number;
  sqlite?: Record<string, unknown>;
};
type Catalog = {
  total: number;
  catalog_total: number;
  updated_at: string | null;
  collection?: { collected: number; failed: number; pending: number } | null;
};
type SectorResult = {
  items: unknown[];
  generated_at?: string;
  method_version?: string;
};
type Universe = {
  version: string;
  generated_at: string;
  items: Array<{ code: string; name: string; index_name: string; observation_count: number; first_date: string; last_date: string }>;
  excluded: Array<{ code: string; reason: string }>;
  selection: string;
};

const capabilityNames: Record<string, string> = {
  fund_browse_codes: '真实基金目录与代码查询',
  sector_refresh_then_evaluate: '板块采集后再评估',
  point_in_time_research_v1: '按时点保存投资研究证据',
  portfolio_ledger_v1: '不可覆盖的本地持仓账本',
  strategy_execution_v1: '交易动作、决定与历史回放',
};

export function RealSettingsPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [sectors, setSectors] = useState<SectorResult | null>(null);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    const results = await Promise.allSettled([
      apiRequest<Health>('/health'),
      apiRequest<Catalog>('/api/funds?page=1&page_size=1'),
      apiRequest<SectorResult>('/api/sectors/opportunities'),
      apiRequest<Universe>('/api/v1/strategy/universe'),
    ]);
    if (results[0].status === 'fulfilled') setHealth(results[0].value);
    if (results[1].status === 'fulfilled') setCatalog(results[1].value);
    if (results[2].status === 'fulfilled') setSectors(results[2].value);
    if (results[3].status === 'fulfilled') setUniverse(results[3].value);
    const failures = results.filter((item) => item.status === 'rejected');
    if (failures.length)
      setError(`${failures.length} 项本地数据状态暂时无法读取；其余已成功返回的资料仍保留显示。`);
    setUpdatedAt(new Date().toLocaleString('zh-CN', { hour12: false }));
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="settings-page real-settings-page">
      <PageHeader
        eyebrow="SYSTEM & DATA"
        title="设置与数据状态"
        description="核对本地服务、数据库版本、真实资料覆盖和当前策略可用范围。"
        actions={<button className="button secondary" disabled={loading} onClick={load}><RefreshCw size={14} />{loading ? '正在读取…' : '刷新状态'}</button>}
      />
      <div className="settings-status-banner">
        <span className="settings-status-icon">{health?.status === 'ok' ? <ShieldCheck size={25} /> : <Database size={25} />}</span>
        <div><h2>{health?.status === 'ok' ? '本地数据服务运行正常' : loading ? '正在连接本地数据服务' : '部分服务状态未取得'}</h2><p>{health ? `接口契约 ${health.api_contract} · 数据库结构版本 ${health.schema_version}` : '请保持桌面应用的本地服务运行。'}{updatedAt && ` · 状态读取于 ${updatedAt}`}</p></div>
        <Badge tone={health?.status === 'ok' ? 'green' : 'amber'}>{health?.status === 'ok' ? '已连接' : '需核对'}</Badge>
      </div>
      {error && <Notice tone="warning">{error}</Notice>}
      <div className="grid-four">
        <Stat label="基金身份目录" value={catalog ? catalog.catalog_total.toLocaleString('zh-CN') : '—'} detail={catalog?.updated_at ? `更新于 ${catalog.updated_at.slice(0, 10)}` : '未取得更新时间'} />
        <Stat label="已采集基金序列" value={catalog?.collection ? catalog.collection.collected.toLocaleString('zh-CN') : '—'} detail={catalog?.collection ? `失败 ${catalog.collection.failed} · 待处理 ${catalog.collection.pending}` : '目录身份与历史序列分别统计'} />
        <Stat label="当前板块研究对象" value={sectors ? `${sectors.items.length} 个` : '—'} detail={sectors?.method_version || '未取得方法版本'} />
        <Stat label="策略可用基金" value={universe ? `${universe.items.length} 只` : '—'} detail={universe ? `另有 ${universe.excluded.length} 只未纳入` : '等待策略服务'} />
      </div>
      <div className="grid-two">
        <Panel title="后端能力" subtitle="能力由当前运行中的本地服务返回。">
          {!health?.capabilities.length ? <p className="muted">尚未取得能力列表。</p> : <ul className="settings-capability-list">{health.capabilities.map((capability) => <li key={capability}><ShieldCheck size={15} /><span>{capabilityNames[capability] || capability}</span><code>{capability}</code></li>)}</ul>}
        </Panel>
        <Panel title="数据计算边界" subtitle="缺少资料时保留未知，不用零值或猜测补齐。">
          <ul className="settings-explanation">
            <li>基金身份、价格或净值、基金与指数关系分别保存来源和更新时间。</li>
            <li>持仓账本确认现金与份额；市场估值使用可获得的最新真实价格或净值。</li>
            <li>策略只覆盖关系已核验且历史序列达到长期窗口要求的基金。</li>
            <li>历史回放保存输入身份、费用、曲线、动作覆盖和异常诊断。</li>
          </ul>
        </Panel>
      </div>
      {universe && (
        <Panel title="当前策略基金范围" subtitle="同一指数只保留一只历史最完整的基金，避免把重复份额误当分散投资。">
          <div className="table-wrap"><table className="data-table"><thead><tr><th>基金</th><th>对应指数</th><th>历史观察数</th><th>覆盖日期</th></tr></thead><tbody>{universe.items.map((item) => <tr key={item.code}><td><Link to={`/funds/${item.code}`}>{item.name}</Link><small>{item.code}</small></td><td>{item.index_name}</td><td>{item.observation_count}</td><td>{item.first_date} 至 {item.last_date}</td></tr>)}</tbody></table></div>
          {!!universe.excluded.length && <details className="settings-excluded"><summary>查看 {universe.excluded.length} 条未纳入原因</summary><ul>{universe.excluded.map((item, index) => <li key={`${item.code}-${index}`}>{item.code}：{item.reason}</li>)}</ul></details>}
        </Panel>
      )}
      <Panel title="资料入口" subtitle="具体数据的刷新与核验在对应业务页面进行。">
        <div className="toolbar"><Link className="button secondary" to="/funds">基金目录与历史</Link><Link className="button secondary" to="/sectors">板块研究与来源</Link><Link className="button secondary" to="/portfolio">正式持仓账本</Link><Link className="button secondary" to="/review">策略历史验证</Link></div>
      </Panel>
    </div>
  );
}
