import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ChevronDown, RefreshCw } from 'lucide-react';
import { Badge, EmptyState, Notice, Panel } from '../../shared/ui';
import './sector-opportunities.css';
import { SectorSummaryTable } from './SectorSummaryTable';
import { SectorHistory } from './SectorHistory';
import { researchHref, subjectKey } from '../advice/research-model';
import { directoryPage } from './fund-directory-model';
import { MarketDataRefresh } from './MarketDataRefresh';
import { sectorDetailHref } from './sector-detail-model';
import './fund-directory.css';

type PeriodStatus = 'strong' | 'neutral' | 'weak' | 'insufficient' | 'stale';
type PeriodRisk = 'elevated' | 'normal' | 'unknown';
type OpportunityStatus = 'watch' | 'conflict' | 'risk' | 'insufficient';
type SortKey = 'heat' | 'short' | 'medium' | 'long';
type SortDirection = 'asc' | 'desc';

export type SectorOpportunityPeriod = {
  id: 'short' | 'medium' | 'long';
  name: '短期' | '中期' | '长期';
  range: string;
  lookback_sessions: number;
  observation_start?: string;
  observation_end?: string;
  status: PeriodStatus;
  label: string;
  reason: string;
  return_pct: number | null;
  ma_bias_pct: number | null;
  max_drawdown_pct: number | null;
  risk: PeriodRisk;
  opportunity: {
    status: OpportunityStatus;
    label: string;
    summary: string;
    supports: string[];
    challenges: string[];
    conditions: string[];
    missing: string[];
  };
  strength?: {
    rank: number | null;
    tied_count?: number;
    percentile: number | null;
    sample_count: number;
    as_of: string | null;
    eligible: boolean;
    reason: string;
  };
};

type EvidenceMetric = {
  id: string;
  label: string;
  latest: {
    period: string;
    value: number | string | null;
    unit: string;
    published_at: string;
    source_url: string | null;
    source_title: string;
  };
  previous: {
    period: string;
    value: number | string | null;
    unit: string;
    published_at: string;
    source_url: string | null;
    source_title: string;
  } | null;
  change_pp: number | null;
};
type IndustryEvidence = {
  status: 'supportive' | 'mixed' | 'pressured' | 'missing' | 'stale';
  label: string;
  summary: string;
  scope: string;
  mapping_note: string;
  metrics: EvidenceMetric[];
};
type ValuationEvidence = {
  status: 'missing' | 'available' | 'not_applicable';
  summary: string;
  pe_ttm?: number | null;
  as_of?: string | null;
  observed_at?: string | null;
  source_url: string | null;
  metrics?: { label: string; value: number | string | null; unit: string }[];
};

export type SectorOpportunity = {
  code: string;
  name: string;
  source_id: string;
  history_source_id?: string | null;
  history_source_code?: string | null;
  history_identity_match?: string | null;
  updated_at: string | null;
  as_of: string | null;
  observation_count: number;
  funds: { code: string; name: string; relation_status?: string; relation_reason?: string }[];
  periods: SectorOpportunityPeriod[];
  industry?: IndustryEvidence;
  valuation?: ValuationEvidence;
  universe_type?: 'hot_board' | 'tracked_index';
  kind?: string;
  heat_rank?: number | null;
  heat_value?: number | null;
  collection_error?: string | null;
  membership?: {
    status: 'ready' | 'failed' | 'missing';
    as_of: string | null;
    fetched_at: string | null;
    member_count: number;
    error: string | null;
    weight_basis: 'not_provided';
    members: {
      stock_code: string;
      stock_name: string;
      market: number;
      source_order: number;
      market_cap: number | null;
      weight: number | null;
    }[];
  };
};

type Universe = {
  label: string;
  heat_basis: string;
  as_of: string | null;
  updated_at: string | null;
  requested_count: number;
  member_count: number;
  collected_count: number;
  failed_count: number;
  source_url: string | null;
  status: 'ready' | 'partial' | 'empty';
  scope?: 'hot_board_top100' | 'verified_industry_all';
  ranking_as_of: string | null;
  last_error?: string | null;
  last_attempt_at?: string | null;
};
type OpportunitiesResponse = {
  coverage?: { universe_type: string; total: number; industry_current: number;
    valuation_observations: number; valuation_dated_current: number;
    price_eligible: { short: number; medium: number; long: number } }[];
  method_version: string;
  generated_at: string;
  items: SectorOpportunity[];
  universe?: Universe;
  advantage_rule?: string;
  advantages?: {
    id: 'short' | 'medium' | 'long';
    name: string;
    horizon: string;
    eligible_count: number;
    comparison_as_of?: string | null;
    candidate_count: number;
    message: string;
    items: {
      code: string;
      name: string;
      label: string;
      reason: string;
      strength_rank: number;
      strength_percentile: number;
      sample_count: number;
      return_pct: number;
      ma_bias_pct: number;
      max_drawdown_pct: number;
      risk: PeriodRisk;
      heat_rank: number | null;
    }[];
  }[];
};

const periodOrder: SectorOpportunityPeriod['id'][] = ['short', 'medium', 'long'];
const historicalRanges = { short: '近1个月', medium: '近3个月', long: '近6个月' };

function formatDate(value: string | null) {
  if (!value) return '暂无';
  return value.slice(0, 10);
}

function formatPercent(value: number | null) {
  return value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function heatBasisLabel(value: string) {
  return value === 'turnover_amount' ? '成交额' : value === 'source_heat' ? '源人气值' : value;
}

function periodFor(item: SectorOpportunity, key: SortKey) {
  return key === 'heat' ? null : (item.periods.find((period) => period.id === key) ?? null);
}

export function sortSectorItems(
  items: SectorOpportunity[],
  key: SortKey,
  direction: SortDirection,
) {
  return [...items].sort((a, b) => {
    if (key === 'heat') {
      const aKnown = a.heat_rank != null && Number.isFinite(a.heat_rank);
      const bKnown = b.heat_rank != null && Number.isFinite(b.heat_rank);
      if (aKnown !== bKnown) return aKnown ? -1 : 1;
      const av = a.heat_rank == null ? Number.POSITIVE_INFINITY : a.heat_rank;
      const bv = b.heat_rank == null ? Number.POSITIVE_INFINITY : b.heat_rank;
      if (av !== bv) return direction === 'desc' ? av - bv : bv - av;
    } else {
      const ap = periodFor(a, key);
      const bp = periodFor(b, key);
      const ae =
        !!ap?.strength?.eligible &&
        typeof ap.return_pct === 'number' &&
        Number.isFinite(ap.return_pct) &&
        ap.status !== 'stale' &&
        ap.status !== 'insufficient';
      const be =
        !!bp?.strength?.eligible &&
        typeof bp.return_pct === 'number' &&
        Number.isFinite(bp.return_pct) &&
        bp.status !== 'stale' &&
        bp.status !== 'insufficient';
      if (ae !== be) return ae ? -1 : 1;
      if (ae && be) {
        const ar = ap?.return_pct ?? Number.NEGATIVE_INFINITY;
        const br = bp?.return_pct ?? Number.NEGATIVE_INFINITY;
        if (ar !== br) return direction === 'desc' ? br - ar : ar - br;
      }
    }
    return a.code.localeCompare(b.code);
  });
}

function statusTone(status: OpportunityStatus): 'green' | 'blue' | 'red' | 'amber' | 'neutral' {
  if (status === 'watch') return 'blue';
  if (status === 'conflict') return 'amber';
  if (status === 'risk') return 'red';
  return 'neutral';
}

function PeriodCard({ period }: { period: SectorOpportunityPeriod }) {
  const unavailable = period.status === 'stale' || period.status === 'insufficient';
  return (
    <div className={`sector-period sector-period-${period.status}`}>
      <div className="sector-period-heading">
        <div>
          <strong>{period.name}</strong>
          <small>
            {historicalRanges[period.id]} · 回看 {period.lookback_sessions} 个交易日
          </small>
        </div>
        <div>
          <Badge
            tone={
              period.status === 'strong' ? 'red' : period.status === 'weak' ? 'green' : 'neutral'
            }
          >
            {period.label}
          </Badge>
          {period.status === 'stale' && <small className="sector-data-warning">行情待更新</small>}
          {period.status === 'insufficient' && (
            <small className="sector-data-warning">行情不足</small>
          )}
        </div>
      </div>
      <div className="sector-strength-line">
        已发生涨跌：{unavailable ? '—' : formatPercent(period.return_pct)} · 同周期排名：
        {period.strength?.eligible && period.strength.rank != null
          ? `第 ${period.strength.rank} / ${period.strength.sample_count}${period.strength.tied_count && period.strength.tied_count > 1 ? `（并列 ${period.strength.tied_count}）` : ''}`
          : period.strength?.reason || '未排名'}
        {period.strength?.percentile != null && period.strength.eligible
          ? `（相对强度分位 ${period.strength.percentile.toFixed(1)}/100，不是上涨概率）`
          : period.strength?.eligible && period.strength.sample_count <= 1
            ? '（单样本，不具备相对分位）'
            : ''}{' '}
        · 比较截止日 {formatDate(period.strength?.as_of ?? null)}
      </div>
      {period.observation_start && period.observation_end && (
        <div className="sector-strength-line">
          实际观察：{period.observation_start} 至 {period.observation_end}
        </div>
      )}
      <div className="sector-investment-line">
        <Badge tone={statusTone(period.opportunity.status)}>
          投资依据：{period.opportunity.label}
        </Badge>{' '}
        未来{period.range}（方向未证实）
      </div>
      <p className="sector-period-summary">{period.opportunity.summary}</p>
      <div className="sector-evidence-columns">
        {!!period.opportunity.supports.length && (
          <div>
            <small>支持</small>
            <ul>
              {period.opportunity.supports.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        )}
        {!!period.opportunity.challenges.length && (
          <div>
            <small>反证与限制</small>
            <ul>
              {period.opportunity.challenges.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {!!(period.opportunity.conditions.length || period.opportunity.missing.length) && (
        <details className="sector-conditions">
          <summary>待确认条件与缺项</summary>
          <ul>
            {[...period.opportunity.conditions, ...period.opportunity.missing].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </details>
      )}
      <details className="sector-price-evidence">
        <summary>走势佐证（回看窗口）</summary>
        <p>{period.reason}</p>
        <div className="sector-period-metrics">
          <span>
            <small>阶段涨跌</small>
            <b>{unavailable ? '—' : formatPercent(period.return_pct)}</b>
          </span>
          <span>
            <small>相对窗口均线</small>
            <b>{unavailable ? '—' : formatPercent(period.ma_bias_pct)}</b>
          </span>
          <span>
            <small>最大回撤</small>
            <b>{unavailable ? '—' : formatPercent(period.max_drawdown_pct)}</b>
          </span>
        </div>
        <div className={`sector-risk sector-risk-${period.risk}`}>
          风险：
          {period.risk === 'elevated'
            ? '回撤较大'
            : period.risk === 'normal'
              ? '窗口回撤未触发提示'
              : '未知'}
        </div>
      </details>
    </div>
  );
}

export function OpportunityCard({ item, heatBasis, from }: { item: SectorOpportunity; heatBasis?: string; from?: string }) {
  const historySource = item.history_source_id ?? item.source_id;
  const historySourceLabel =
    historySource === 'sector_daily.ths'
      ? '同花顺日线'
      : historySource === 'sector_daily.eastmoney'
        ? '东方财富日线'
        : historySource;
  const periods = periodOrder
    .map((id) => item.periods.find((period) => period.id === id))
    .filter(Boolean) as SectorOpportunityPeriod[];
  return (
    <Panel className="sector-opportunity-card">
      <div className="sector-card-heading">
        <div>
          <h2>
            <Link to={sectorDetailHref(item, from)}>{item.name}</Link>
          </h2>
          <span>
            {item.code} · 行情 {historySourceLabel ?? '暂缺'}
            {item.history_source_code ? ` (${item.history_source_code})` : ''}
          </span>
        </div>
        <span className="sector-observation">{item.observation_count} 条观察</span>
      </div>
      <div className="sector-card-meta">
        <span>数据日期：{formatDate(item.as_of)}</span>
        <span>更新于：{formatDate(item.updated_at)}</span>
        <span>
          {item.universe_type === 'tracked_index'
            ? '已有指数观察'
            : `热度 #${item.heat_rank ?? '—'}`}
        </span>
        {item.heat_value != null && item.universe_type === 'hot_board' && (
          <span>
            {heatBasis === 'turnover_amount'
              ? `成交额 ${(item.heat_value / 1e8).toFixed(2)} 亿元`
              : `源人气值 ${item.heat_value}`}
          </span>
        )}
      </div>
      {item.collection_error && (
        <Notice title="该板块行情更新未完成" tone="warning">
          已有行情仅供核对，本次不参与强度排名。
          <details>
            <summary>查看采集原因</summary>
            {item.collection_error}
          </details>
        </Notice>
      )}
      {item.membership && (
        <details className="sector-membership">
          <summary>
            成分股 · {item.membership.member_count} 只 · 快照日期{' '}
            {formatDate(item.membership.as_of)}
          </summary>
          {item.membership.status === 'ready' ? (
            <>
              <p>来源未提供板块官方权重，以下按来源返回顺序展示，不把总市值当作权重。</p>
              <div className="sector-member-list">
                {item.membership.members.slice(0, 10).map((member) => (
                  <span key={`${member.market}-${member.stock_code}`}>
                    {member.stock_name} <small>{member.stock_code}</small>
                  </span>
                ))}
              </div>
              <p className="muted">
                已显示 {Math.min(10, item.membership.members.length)} /{' '}
                {item.membership.member_count} 只。
              </p>
              {item.membership.members.length > 10 && (
                <details>
                  <summary>查看其余 {item.membership.members.length - 10} 只成分股</summary>
                  <div className="sector-member-list">
                    {item.membership.members.slice(10).map((member) => (
                      <span key={`${member.market}-${member.stock_code}`}>
                        {member.stock_name} <small>{member.stock_code}</small>
                      </span>
                    ))}
                  </div>
                </details>
              )}
            </>
          ) : (
            <p>{item.membership.error || '成分股资料尚未采集。'}</p>
          )}
        </details>
      )}
      <div className="sector-periods">
        {periods.length ? (
          periods.map((period) => <PeriodCard key={period.id} period={period} />)
        ) : (
          <div className="sector-missing">暂无周期资料</div>
        )}
      </div>
      {item.industry && (
        <details className="sector-industry-evidence">
          <summary>行业证据 · {item.industry.label}</summary>
          <p>{item.industry.summary}</p>
          <small>
            {item.industry.scope} · {item.industry.mapping_note} · 同比 pp，非月环比
          </small>
          {item.industry.metrics.length > 0 && (
            <div className="sector-evidence-metrics">
              {item.industry.metrics.map((metric) => (
                <div key={metric.id}>
                  <strong>{metric.label}</strong>
                  <span>
                    前期：{metric.previous?.value ?? '—'}
                    {metric.previous?.unit ?? metric.latest.unit}（
                    {formatDate(metric.previous?.published_at ?? null)}）
                  </span>
                  <span>
                    最新：{metric.latest.value ?? '—'}
                    {metric.latest.unit}（{formatDate(metric.latest.published_at)}）
                  </span>
                  <span>
                    同比变化：
                    {metric.change_pp == null
                      ? '—'
                      : `${metric.change_pp > 0 ? '+' : ''}${metric.change_pp}pp`}
                  </span>
                  {metric.latest.source_url && /^https?:\/\//.test(metric.latest.source_url) ? (
                    <a href={metric.latest.source_url} target="_blank" rel="noreferrer">
                      {metric.latest.source_title}
                    </a>
                  ) : (
                    <small>{metric.latest.source_title}</small>
                  )}
                </div>
              ))}
            </div>
          )}
        </details>
      )}
      {!item.industry && (
        <div className="sector-industry-missing">
          缺少该板块行业经营与估值证据，暂不能给投资方向。
        </div>
      )}
      {item.valuation && (
        <div className="sector-valuation">
          <strong>估值</strong>
          <span>
            {item.valuation.status === 'not_applicable'
              ? '不适用'
              : item.valuation.status === 'missing'
                ? '缺少已核验估值'
                : '已提供原始观察'}
          </span>
          {item.valuation.metrics?.map((metric) => (
            <span key={metric.label}>
              {metric.label}：{metric.value ?? '—'}
              {metric.unit}
            </span>
          ))}
          <small>
            {item.valuation.as_of
              ? `估值日期 ${formatDate(item.valuation.as_of)}`
              : '估值业务日期未披露'}
          </small>
          {item.valuation.observed_at && (
            <small>资料保存于 {formatDate(item.valuation.observed_at)}</small>
          )}
          {item.valuation.source_url && /^https?:\/\//.test(item.valuation.source_url) && (
            <a href={item.valuation.source_url} target="_blank" rel="noreferrer">
              来源
            </a>
          )}
          <em>{item.valuation.summary}</em>
        </div>
      )}
      <SectorHistory item={item} />
      {item.funds.length > 0 && (
        <div className="sector-related-funds">
          <span>已核验相关基金走势</span>
          {item.funds.some((fund) => fund.relation_status !== 'linked') && (
            <p>部分历史关联待核验，不作为当前候选；详情见研究页。</p>
          )}
          <div>
            {item.funds
              .filter((fund) => fund.relation_status === 'linked')
              .map((fund) => (
                <Link key={fund.code} to={`/funds/${encodeURIComponent(fund.code)}`}>
                  {fund.name} <small>{fund.code}</small>
                </Link>
              ))}
          </div>
        </div>
      )}
      <p className="sector-card-footnote">
        指数价格变化不等于基金回报，相关基金链接仅用于查看走势，不构成推荐。
      </p>
    </Panel>
  );
}

export function SectorOpportunities() {
  const [data, setData] = useState<OpportunitiesResponse | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [showMethod, setShowMethod] = useState(false);
  const [params, setParams] = useSearchParams();
  const query = params.get('q') ?? '';
  const scope: 'hot' | 'all' | 'tracked' =
    params.get('scope') === 'all' ? 'all' : params.get('scope') === 'tracked' ? 'tracked' : 'hot';
  const sortKey: SortKey = ['short', 'medium', 'long'].includes(params.get('sort') ?? '')
    ? (params.get('sort') as SortKey)
    : 'heat';
  const direction: SortDirection = params.get('direction') === 'asc' ? 'asc' : 'desc';
  const page = directoryPage(params.get('page'));
  const view = params.get('view') === 'details' ? 'details' : 'summary';
  const updateFilters = (values: Record<string, string>, replace = false) => {
    const next = new URLSearchParams(params);
    if (Object.keys(values).some((key) => key !== 'view')) next.delete('page');
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace });
  };
  const setQuery = (value: string) => updateFilters({ q: value }, true);
  const setScope = (value: typeof scope) => updateFilters({ scope: value });
  const setSortKey = (value: SortKey) => updateFilters({ sort: value });
  const setDirection = (value: SortDirection) => updateFilters({ direction: value });
  const setPage = (value: number | ((previous: number) => number)) => {
    const next = new URLSearchParams(params);
    next.set('page', String(typeof value === 'function' ? value(page) : value));
    setParams(next);
  };
  const request = useRef<AbortController | null>(null);
  const hasData = useRef(false);
  const load = useCallback((isRefresh = false) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    if (isRefresh) setRefreshing(true);
    if (!hasData.current) setState('loading');
    setLoadError('');
    fetch('/api/sectors/opportunities', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = (await response.json()) as OpportunitiesResponse;
        if (!result || !Array.isArray(result.items)) throw new Error('板块接口格式无效');
        return result;
      })
      .then((result) => {
        if (controller.signal.aborted) return;
        hasData.current = true;
        setData(result);
        setState('ready');
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setLoadError('请确认本地数据服务已启动，或稍后重试。');
        setState(hasData.current ? 'ready' : 'error');
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefreshing(false);
      });
  }, []);

  const sourceItems = data?.items ?? [];
  const sortedItems = sortSectorItems(sourceItems, sortKey, direction);
  const filteredItems = sortedItems.filter((item) => {
    if (scope === 'hot' && item.universe_type === 'tracked_index') return false;
    if (scope === 'tracked' && item.universe_type !== 'tracked_index') return false;
    const normalized = query.trim().toLowerCase();
    return (
      !normalized ||
      item.name.toLowerCase().includes(normalized) ||
      item.code.toLowerCase().includes(normalized)
    );
  });
  const pageSize = 10;
  const pageCount = Math.max(1, Math.ceil(filteredItems.length / pageSize));
  const visibleItems = filteredItems.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => {
    if (data && state === 'ready' && page > pageCount) {
      const next = new URLSearchParams(params);
      next.set('page', String(pageCount));
      setParams(next, { replace: true });
    }
  }, [data, state, page, pageCount, params, setParams]);

  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, [load]);

  return (
    <div className="sector-opportunities">
      <Notice title="真实板块行情与分层证据，不是基金推荐或买卖信号" tone="info">
        走势可比较；行业/估值资料按实际覆盖显示，不套用其他板块资料。首版机会筛查与预测效果尚未验证。
      </Notice>
      <div className="sector-toolbar">
        <p>
          读取最新本地行情与已核验行业快照。
          {data && (
            <small className="sector-method-version">
              评估于{' '}
              {new Date(data.generated_at).toLocaleString('zh-CN', {
                timeZone: 'Asia/Shanghai',
                hour12: false,
              })}{' '}
              · {data.method_version}
            </small>
          )}
        </p>
        <button
          className="button secondary"
          type="button"
          onClick={() => load(true)}
          disabled={refreshing || state === 'loading'}
        >
          <RefreshCw size={15} className={refreshing ? 'sector-spin' : ''} />
          {refreshing ? '重新评估中…' : '重新评估'}
        </button>
      </div>
      <MarketDataRefresh target="sectors" onSettled={() => load(true)} />
      <p className="muted">“获取最新行业数据”联网采集名单、日线和成分，完成后自动重读评估；“重新评估”只读本地资料。经营／估值人工快照不会自动刷新；已有参考指数请从关联基金详情更新。</p>
      {data?.coverage && <section className="sector-coverage" aria-label="板块研究数据覆盖">
        <strong>当前资料覆盖，不是投资评分</strong>
        {data.coverage.map((coverage) => <p key={coverage.universe_type}>
          {coverage.universe_type === 'hot_board' ? '行业／板块池' : '参考指数池'}：共 {coverage.total} 个；
          短／中／长期可比较 {coverage.price_eligible.short}／{coverage.price_eligible.medium}／{coverage.price_eligible.long} 个；
          有效经营依据 {coverage.industry_current} 个；估值原始观察 {coverage.valuation_observations} 个，其中日期明确且未过期 {coverage.valuation_dated_current} 个。
        </p>)}
        <p>尚缺同口径历史估值、成分盈利与现金流穿透、资金申赎及催化／预期快照、完整交易日历和样本外验证；本页面不生成正式投资推荐。</p>
      </section>}
      {data?.universe && (
        <div className="sector-universe-meta">
          <strong>{data.universe.label}</strong>
          <span>
            口径：{heatBasisLabel(data.universe.heat_basis)}
            {data.universe.heat_basis === 'turnover_amount' ? '（成交额单位：亿元）' : ''}
          </span>
          <span>
            目标 {data.universe.requested_count} 个 · 已取得名单 {data.universe.member_count} 个 ·
            行情成功 {data.universe.collected_count} 个 · 行情失败 {data.universe.failed_count} 个
          </span>
          <span>榜单日期：{formatDate(data.universe.ranking_as_of)}</span>
          {data.universe.source_url && /^https?:\/\//.test(data.universe.source_url) && (
            <a href={data.universe.source_url} target="_blank" rel="noreferrer">
              榜来源
            </a>
          )}
          <small>热度不等于走势强度或投资价值；分类可能重叠，热度有规模偏向。</small>
          {data.universe.last_error && (
            <Notice tone="warning" title="最近一次榜单更新未完成">
              {data.universe.member_count
                ? '当前展示上次成功取得的榜单，请以榜单日期为准。'
                : '数据源尚未返回当前范围的完整名单。'}
              <details>
                <summary>查看采集原因</summary>
                {data.universe.last_error}
              </details>
            </Notice>
          )}
        </div>
      )}
      {data?.advantages && (
        <section className="sector-advantage-section" aria-labelledby="sector-advantage-title">
          <div className="sector-advantage-heading">
            <div>
              <h2 id="sector-advantage-title">三周期优势板块</h2>
              <p>
                优势表示截至各周期价格比较日的走势相对领先，不能直接推导未来上涨或基金买入结论。
              </p>
            </div>
            <small>{data.advantage_rule}</small>
          </div>
          <div className="sector-advantage-grid">
            {data.advantages.map((summary) => (
              <Panel key={summary.id} className="sector-advantage-panel">
                <div className="sector-advantage-period">
                  <div>
                    <strong>{summary.name}</strong>
                    <small>{summary.horizon}</small>
                  </div>
                  <span>
                    {summary.eligible_count} 个可比较 · 价格比较截至{' '}
                    {formatDate(summary.comparison_as_of ?? null)}
                  </span>
                </div>
                <p>{summary.message}</p>
                {summary.items.length ? (
                  <ol>
                    {summary.items.map((item) => (
                      <li key={item.code}>
                        <button
                          type="button"
                          onClick={() =>
                            updateFilters({
                              q: item.code,
                              scope: 'hot',
                              sort: summary.id,
                              direction: 'desc',
                            })
                          }
                        >
                          <span>
                            <b>{item.name}</b>
                            <em>{item.label}</em>
                          </span>
                          <small>
                            同榜第 {item.strength_rank}/{item.sample_count} · 涨跌{' '}
                            {formatPercent(item.return_pct)}
                          </small>
                          <small>
                            均线偏离 {formatPercent(item.ma_bias_pct)} · 最大回撤{' '}
                            {formatPercent(item.max_drawdown_pct)}
                          </small>
                        </button>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <span className="sector-advantage-empty">本周期不勉强给出优势板块。</span>
                )}
              </Panel>
            ))}
          </div>
        </section>
      )}
      {data && loadError && (
        <div className="notice notice-warning" role="alert">
          <strong>本次重新评估失败，保留上次完整结果。</strong>
          <p>
            {loadError} 当前结果读取于 {data.generated_at}；行情日期仍以各板块为准。
          </p>
        </div>
      )}
      <div className="sector-view-controls" aria-label="板块展示方式">
        <button
          className="button secondary"
          aria-pressed={view === 'summary'}
          onClick={() => updateFilters({ view: 'summary' })}
        >
          三周期摘要
        </button>
        <button
          className="button secondary"
          aria-pressed={view === 'details'}
          onClick={() => updateFilters({ view: 'details' })}
        >
          完整证据
        </button>
        <span className="muted">摘要用于比较，完整证据保留原始指标、来源及反证。</span>
      </div>
      <div className="sector-controls">
        <input
          aria-label="搜索板块名称或代码"
          placeholder="搜索板块名称或代码"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          aria-label="板块范围"
          value={scope}
          onChange={(event) => setScope(event.target.value as typeof scope)}
        >
          <option value="hot">
            {data?.universe?.scope === 'verified_industry_all' ? '全部可核验行业' : '热门前100'}
          </option>
          <option value="all">全部已采集</option>
          <option value="tracked">已有指数观察</option>
        </select>
        <select
          aria-label="排序字段"
          value={sortKey}
          onChange={(event) => setSortKey(event.target.value as SortKey)}
        >
          <option value="heat">热度</option>
          <option value="short">短期走势强度 · 近1个月</option>
          <option value="medium">中期走势强度 · 近3个月</option>
          <option value="long">长期走势强度 · 近6个月</option>
        </select>
        <select
          aria-label="排序方向"
          value={direction}
          onChange={(event) => setDirection(event.target.value as SortDirection)}
        >
          <option value="desc">从强到弱</option>
          <option value="asc">从弱到强</option>
        </select>
      </div>
      <p className="sector-scope-note">
        排序依据：同一实际区间的价格涨跌幅；数值越大，历史走势越强。
        短期回看20、中期60、长期120个交易日，约为1、3、6个月。
        投资依据另看未来1周至1个月、1至3个月、3至6个月；本页“长期”指半年内。
      </p>
      {scope === 'all' && (
        <p className="sector-scope-note">
          全部已采集包含热门板块与已有指数观察；两类榜单的排名样本不同，参考指数不纳入热门榜排名。
        </p>
      )}
      {state === 'loading' && (
        <div className="sector-state" role="status">
          正在读取真实指数观察…
        </div>
      )}
      {state === 'error' && (
        <EmptyState
          title="真实指数观察暂时无法加载"
          description="请确认本地数据服务已启动后重试。"
          action={
            <button className="button primary" type="button" onClick={() => load()}>
              重试
            </button>
          }
        />
      )}
      {state === 'ready' && data && !data.items.length && (
        <EmptyState
          title="暂时没有真实指数观察"
          description="本地数据服务尚未返回可展示的指数周期资料。"
        />
      )}
      {state === 'ready' &&
        data &&
        data.items.length > 0 &&
        visibleItems.length > 0 &&
        (view === 'summary' ? (
          <SectorSummaryTable items={visibleItems} from={`/sectors?${params}`} />
        ) : (
          <div className="sector-opportunity-list">
            {visibleItems.map((item) => (
              <OpportunityCard
                key={subjectKey(item)}
                item={item}
                heatBasis={data.universe?.heat_basis}
                from={`/sectors?${params}`}
              />
            ))}
          </div>
        ))}
      {state === 'ready' && data && data.items.length > 0 && !visibleItems.length && (
        <EmptyState
          title={
            scope === 'hot' && data.universe?.member_count === 0
              ? '热门榜尚未采集成功'
              : '没有匹配的板块'
          }
          description={
            scope === 'hot' && data.universe?.member_count === 0
              ? '可切换“已有指数观察”查看原有资料，榜单采集完成后再重新评估。'
              : '请调整搜索词或筛选范围。'
          }
        />
      )}
      {state === 'ready' && data && filteredItems.length > 0 && (
        <div className="sector-pagination">
          <span>
            显示 {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, filteredItems.length)} /{' '}
            {filteredItems.length}
          </span>
          <button
            className="button secondary"
            disabled={page <= 1}
            onClick={() => setPage((value) => value - 1)}
          >
            上一页
          </button>
          <button
            className="button secondary"
            disabled={page >= pageCount}
            onClick={() => setPage((value) => value + 1)}
          >
            下一页
          </button>
        </div>
      )}
      <div className="sector-method">
        <button
          type="button"
          onClick={() => setShowMethod((value) => !value)}
          aria-expanded={showMethod}
        >
          <span>方法说明与资料边界</span>
          <ChevronDown size={17} className={showMethod ? 'sector-chevron-open' : ''} />
        </button>
        {showMethod && (
          <div className="sector-method-body">
            <p>
              三周期分别观察近期催化、中期兑现、3–6 月持续性与估值；20/60/120
              交易日仅作为走势佐证窗口。价格偏强需阶段涨幅超过 1% 且高于均线，偏弱需低于 -1%
              且低于均线；窗口回撤不高于 -10% 触发风险提示。
            </p>
            <p>
              最新行情距计算日超过 10 个自然日，或窗口内行情间隔超过 14
              个自然日时暂停走势判断；行业数据发布超过 45
              个自然日时暂停经营判断。相邻累计报告期并非独立验证。以上是首版初始参数，预测效果尚未验证。
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default SectorOpportunities;
