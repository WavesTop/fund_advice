import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  ChartNoAxesCombined,
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  Filter,
  Plus,
  Search,
} from 'lucide-react';
import type { EChartsOption } from 'echarts';
import { DATA_DATE, periods, signed } from '../../shared/data';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Stat, Tabs } from '../../shared/ui';
import { Chart } from '../../shared/Chart';
import { useDemo } from '../../shared/store';
import {
  filterFunds,
  makeSeries,
  marketFunds,
  resolvePage,
  restrictSeries,
  sectorDefinitions,
  validateFundCode,
} from './model';
import type { MarketFund } from './model';
import type { Transaction } from '../../shared/types';
import './market.css';
import { KlinePanel, KlinePreview } from './KlineChart';
import { SectorOpportunities } from './SectorOpportunities';
import { FundDirectory as RealFundDirectory } from './FundDirectory';
import { researchHref, safeReturnPath, sourceHref } from '../advice/research-model';
import { commonDates, alignRows, rangeDates } from './series-alignment';
import { RealSectorDetail } from './RealSectorDetail';

const dateOf = (fund: MarketFund) => (fund.historical ? '2025-12-31' : DATA_DATE);
const coverageTone = (coverage: string) =>
  coverage === '完整' ? 'green' : coverage === '待补全' ? 'neutral' : 'amber';

function recordedFundCodes(transactions: Transaction[]) {
  return new Set(transactions.map((transaction) => transaction.fundCode));
}

function MarketNavigation({ selected }: { selected: 'funds' | 'sectors' }) {
  return (
    <nav className="market-subnav" aria-label="基金与板块导航">
      <Link className={selected === 'funds' ? 'active' : ''} to="/funds">
        <Database size={16} />
        基金库
      </Link>
      <Link className={selected === 'sectors' ? 'active' : ''} to="/sectors">
        <ChartNoAxesCombined size={16} />
        板块机会
      </Link>
    </nav>
  );
}

function MarketState() {
  const { state } = useDemo();
  if (state.scenario === 'loading')
    return (
      <Notice title="正在准备基金目录" tone="info">
        目录初始化场景：已保存的示例基金可以继续浏览，资料查询和分析尚未开始。
      </Notice>
    );
  if (state.scenario === 'offline')
    return (
      <Notice title="当前无法连接本地服务" tone="warning">
        以下为浏览器已保存的演示资料，日期 {DATA_DATE}。新的更新请求暂不可提交。
        <Link to="/settings?tab=data">查看连接状态</Link>
      </Notice>
    );
  if (state.scenario === 'error')
    return (
      <Notice title="资料更新失败" tone="error">
        演示失败场景：保留上次资料，未产生新的行情或分析结果。
        <Link to="/settings?tab=data">查看原因与重试入口</Link>
      </Notice>
    );
  if (state.scenario === 'stale')
    return (
      <Notice title="资料已过期 · 请先核对日期" tone="warning">
        旧资料仍可浏览，本次没有生成新的分析。请更新受影响基金后再评估。
        <Link to="/settings?tab=data">查看待更新资料</Link>
      </Notice>
    );
  return null;
}

type RealFund = {
  share_id: string;
  code: string;
  name: string;
  fund_type: string;
  source_id: string;
};

type RealFundResponse = {
  items: RealFund[];
  page: number;
  page_size: number;
  total: number;
  catalog_total: number;
  collection?: {
    collected: number;
    failed: number;
    pending: number;
  } | null;
  updated_at: string | null;
};

type RealPriceRow = {
  date: string;
  open: string | null;
  high: string | null;
  low: string | null;
  close: string | null;
  volume: string | null;
  amount: string | null;
};

type RealNavRow = {
  date: string;
  unit_nav: string | null;
  accumulated_nav?: string | null;
  cumulative_nav?: string | null;
};

type RealFundSeriesResponse = {
  kind: 'price' | 'nav' | null;
  rows: RealPriceRow[] | RealNavRow[];
  source_id: string | null;
  policy_version: string | null;
  updated_at: string | null;
};

type RealFundDetailResponse = {
  fund: RealFund;
  series: RealFundSeriesResponse | null;
  related_market?: RelatedMarket | null;
  related_markets?: RelatedMarket[];
  series_options?: RealFundSeriesResponse[];
  refresh?: {
    status: 'success' | 'partial' | 'failed';
    stages: Record<string, { status: string; message: string }>;
  };
};

type RelatedMarket = {
  name: string;
  code: string;
  kind: 'index';
  source_id: string;
  relation_status?: 'linked' | 'withheld' | 'superseded';
  relation_source_id?: string;
  verified_at?: string;
  evidence_url?: string;
  relation_reason?: string;
  updated_at?: string;
  rows: RealPriceRow[];
};

function RealFundCatalog() {
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<RealFundResponse | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('loading');
  const [error, setError] = useState('');
  const pageSize = 20;

  useEffect(() => {
    const controller = new AbortController();
    setStatus('loading');
    setError('');
    const params = new URLSearchParams({
      q: query,
      page: String(page),
      page_size: String(pageSize),
    });
    fetch(`/api/funds?${params.toString()}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return (await response.json()) as RealFundResponse;
      })
      .then((data) => {
        setResult(data);
        setStatus('idle');
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setStatus('error');
        setError('本地真实基金目录暂时无法加载，请确认本地数据服务已启动。');
      });
    return () => controller.abort();
  }, [query, page]);

  const totalPages = Math.max(1, Math.ceil((result?.total ?? 0) / pageSize));
  return (
    <Panel className="real-fund-catalog">
      <div className="market-list-heading">
        <div>
          <h2>
            本地真实基金目录 <span className="market-count">数据源目录</span>
          </h2>
          <p className="muted">来自本地数据服务的基金身份信息；不包含演示行情、净值或投资建议。</p>
        </div>
        <span className="market-date">
          <span className="market-dot" />
          本地数据服务
        </span>
      </div>
      <div className="market-search-row">
        <label className="market-search">
          <Search size={18} />
          <input
            type="search"
            aria-label="搜索本地真实基金"
            placeholder="搜索名称或代码"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
          />
        </label>
      </div>
      {status === 'loading' && (
        <div className="real-catalog-state" role="status">
          正在加载本地真实基金目录…
        </div>
      )}
      {status === 'error' && (
        <div className="real-catalog-state" role="alert">
          {error}
        </div>
      )}
      {status === 'idle' && result && !result.items.length && (
        <div className="real-catalog-state">没有找到匹配的本地真实基金。</div>
      )}
      {status === 'idle' && result && result.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table className="data-table real-fund-table">
              <thead>
                <tr>
                  <th>基金名称</th>
                  <th>代码</th>
                  <th>基金类型</th>
                  <th>来源</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((fund) => (
                  <tr key={fund.share_id}>
                    <td>
                      <strong>{fund.name}</strong>
                      <small className="market-fund-meta">份额 ID：{fund.share_id}</small>
                    </td>
                    <td>{fund.code}</td>
                    <td>{fund.fund_type || '—'}</td>
                    <td>{fund.source_id || '—'}</td>
                    <td>
                      <span className="muted">详情待接入</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="market-pagination">
            <span>
              共 {result.total} 条 · 第 {result.page} / {totalPages} 页
              {result.updated_at ? ` · 更新于 ${result.updated_at}` : ''}
            </span>
            <div>
              <button
                className="button secondary"
                aria-label="本地目录上一页"
                disabled={page <= 1}
                onClick={() => setPage((value) => value - 1)}
              >
                <ChevronLeft size={16} />
              </button>
              <button
                className="button secondary"
                aria-label="本地目录下一页"
                disabled={page >= totalPages}
                onClick={() => setPage((value) => value + 1)}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}

function AddFund({ initialCode, onClose }: { initialCode: string; onClose: () => void }) {
  const [code, setCode] = useState(initialCode);
  const [error, setError] = useState('');
  const [identity, setIdentity] = useState<MarketFund>();
  const { state } = useDemo();
  function lookup(event: React.FormEvent) {
    event.preventDefault();
    setIdentity(undefined);
    const invalid = validateFundCode(code);
    if (invalid) return setError(invalid);
    if (state.scenario === 'offline' || state.scenario === 'error')
      return setError('资料查询当前不可用。输入已保留，请恢复连接后再试。');
    const found = marketFunds.find((fund) => fund.code === code.trim());
    if (!found)
      return setError(
        '演示目录未收录此代码。真实基金身份查询尚未接入，无法确认名称和份额类别；本次没有新增基金或任务。',
      );
    setError('');
    setIdentity(found);
  }
  return (
    <Modal title="按基金代码补充" onClose={onClose} dirty={code !== initialCode}>
      <div className="market-steps">
        <span className="active">1 输入代码</span>
        <ChevronRight size={14} />
        <span className={identity ? 'active' : ''}>2 确认基金身份</span>
        <ChevronRight size={14} />
        <span>3 查看资料</span>
      </div>
      <p className="muted">请核对代码、名称及份额类别。演示目录仅支持核对已收录的示例身份。</p>
      <form onSubmit={lookup}>
        <label className="field">
          基金代码
          <input
            value={code}
            inputMode="numeric"
            placeholder="例如 000001"
            onChange={(event) => {
              setCode(event.target.value);
              setIdentity(undefined);
              setError('');
            }}
            aria-invalid={!!error}
            aria-describedby={error ? 'lookup-error' : undefined}
          />
        </label>
        {error && (
          <p role="alert" id="lookup-error" className="market-error">
            {error}
          </p>
        )}
        <button className="button primary" type="submit">
          <Search size={16} />
          查询演示身份
        </button>
      </form>
      {identity && (
        <div className="market-identity-result">
          <Badge tone="green">
            <Check size={13} />
            已收录，无需重复补录
          </Badge>
          <h3>
            {identity.name} <span className="market-share">{identity.share}</span>
          </h3>
          <p>
            {identity.code} · {identity.type} ·{' '}
            {identity.historical
              ? '历史基金，已停止运作'
              : identity.coverage === '完整'
                ? '基础资料可用，演示分析可查看'
                : '基础资料可用，分析资料尚有缺口'}
          </p>
          <Link className="button primary" to={`/funds/${identity.code}`}>
            确认身份，进入详情
            <ArrowRight size={16} />
          </Link>
        </div>
      )}
    </Modal>
  );
}

function LegacyFundsPage() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const [adding, setAdding] = useState(false);
  const { state } = useDemo();
  const held = useMemo(() => recordedFundCodes(state.transactions), [state.transactions]);
  const filters = {
    query: params.get('q') ?? '',
    type: params.get('type') ?? '',
    sector: params.get('sector') ?? '',
    coverage: params.get('coverage') ?? '',
    sort: params.get('sort') ?? 'code',
    onlyHeld: params.get('held') === '1',
  };
  const matching = filterFunds(marketFunds, filters, held);
  const pageSize = 6;
  const page = resolvePage(params.get('page'), matching.length, pageSize);
  const displayed = matching.slice((page - 1) * pageSize, page * pageSize);
  function changeFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== 'page') next.delete('page');
    setParams(next, { replace: key === 'q' });
  }
  useEffect(() => {
    const position = sessionStorage.getItem(`market-scroll:${location.search}`);
    if (!position) return;
    const frame = requestAnimationFrame(() => window.scrollTo({ top: Number(position) }));
    return () => cancelAnimationFrame(frame);
  }, [location.search]);
  const rememberScroll = () =>
    sessionStorage.setItem(`market-scroll:${location.search}`, String(window.scrollY));
  const returnPath = `/funds${location.search}`;
  return (
    <div className="market-page">
      <PageHeader
        eyebrow="MARKET EXPLORER"
        title="发现值得了解的基金"
        description="从基金与板块出发，让每一次判断都有据可循。"
        actions={
          <button className="button primary" onClick={() => setAdding(true)}>
            <Plus size={17} />
            补充基金
          </button>
        }
      />
      <MarketNavigation selected="funds" />
      <MarketState />
      <RealFundCatalog />
      <KlinePreview />
      <div className="market-overview">
        <Stat
          label="演示基金目录"
          value={String(marketFunds.length)}
          detail="同名份额独立展示 · 包含历史基金"
        />
        <Stat
          label="基础资料完整"
          value={String(marketFunds.filter((fund) => fund.coverage === '完整').length)}
          detail="仅说明演示资料覆盖，不代表投资评级"
        />
        <Stat label="覆盖行业与主题" value="7" detail="行业与主题分开识别" />
        <Stat label="资料反映日期" value={DATA_DATE} detail="各基金实际截止日期见列表" />
      </div>
      <Panel className="market-list-panel">
        <div className="market-list-heading">
          <div>
            <h2>
              基金库 <span className="market-count">{matching.length}</span>
            </h2>
            <p className="muted">
              {filters.sort === 'change'
                ? '按日变化从高到低排列，历史基金置后'
                : filters.sort === 'name'
                  ? '按基金名称排列'
                  : '按基金代码升序排列'}
              ；准确代码匹配优先。
            </p>
          </div>
          <span className="market-date">
            <span className="market-dot" />
            本地演示目录
          </span>
        </div>
        <div className="market-search-row">
          <label className="market-search">
            <Search size={18} />
            <input
              type="search"
              aria-label="搜索基金名称或代码"
              placeholder="搜索基金名称、代码或曾用名"
              value={filters.query}
              onChange={(event) => changeFilter('q', event.target.value)}
            />
            <kbd>搜索</kbd>
          </label>
          <label className="market-held">
            <input
              type="checkbox"
              checked={filters.onlyHeld}
              onChange={(event) => changeFilter('held', event.target.checked ? '1' : '')}
            />
            仅看有我的记录
          </label>
        </div>
        <div className="market-filters">
          <Filter size={15} aria-hidden="true" />
          <label>
            <span className="sr-only">基金类型</span>
            <select
              aria-label="基金类型"
              value={filters.type}
              onChange={(event) => changeFilter('type', event.target.value)}
            >
              <option value="">全部类型</option>
              {['指数型', '混合型', 'ETF联接'].map((type) => (
                <option key={type}>{type}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">行业与主题</span>
            <select
              aria-label="行业与主题"
              value={filters.sector}
              onChange={(event) => changeFilter('sector', event.target.value)}
            >
              <option value="">全部行业 / 主题</option>
              {sectorDefinitions.map((sector) => (
                <option key={sector.name}>{sector.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">资料覆盖</span>
            <select
              aria-label="资料覆盖"
              value={filters.coverage}
              onChange={(event) => changeFilter('coverage', event.target.value)}
            >
              <option value="">全部资料状态</option>
              {['完整', '部分资料', '待补全'].map((coverage) => (
                <option key={coverage}>{coverage}</option>
              ))}
            </select>
          </label>
          <label className="market-sort">
            排序
            <select
              aria-label="基金排序"
              value={filters.sort}
              onChange={(event) => changeFilter('sort', event.target.value)}
            >
              <option value="code">基金代码</option>
              <option value="change">日变化从高到低</option>
              <option value="name">基金名称</option>
            </select>
          </label>
          {location.search && (
            <button className="button secondary market-reset" onClick={() => setParams({})}>
              重置
            </button>
          )}
        </div>
        {!displayed.length ? (
          <EmptyState
            title={filters.onlyHeld ? '没有符合条件的个人记录' : '没有找到匹配的基金'}
            description={
              filters.onlyHeld
                ? '尚未录入持仓也可以浏览完整基金库，关闭筛选即可继续查看。'
                : '可以调整筛选，或通过六位代码核对基金身份。搜索内容已保留。'
            }
            action={
              <button
                className="button secondary"
                onClick={() => (filters.onlyHeld ? changeFilter('held', '') : setAdding(true))}
              >
                {filters.onlyHeld ? '浏览全部基金' : '按基金代码补充'}
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table market-fund-table">
              <thead>
                <tr>
                  <th>基金名称 / 代码</th>
                  <th>基金类型</th>
                  <th>净值 / 交易价格</th>
                  <th>日变化</th>
                  <th>相关板块</th>
                  <th>资料状态</th>
                  <th>
                    <span className="sr-only">查看</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {displayed.map((fund) => (
                  <tr key={fund.code}>
                    <td>
                      <div className="market-fund-identity">
                        <span
                          className="market-fund-icon"
                          style={{ color: fund.color, background: `${fund.color}12` }}
                        >
                          {fund.type === '指数型' ? '指' : fund.type === 'ETF联接' ? '联' : '混'}
                        </span>
                        <div>
                          <Link
                            className="market-fund-title"
                            onClick={rememberScroll}
                            to={`/funds/${fund.code}?from=${encodeURIComponent(returnPath)}`}
                          >
                            {fund.name}
                            <span className="market-share">{fund.share}</span>
                          </Link>
                          <p className="market-fund-meta">
                            {fund.code}
                            {held.has(fund.code) && <span> · 有我的记录</span>}
                            {fund.historical && <span> · 已停止运作</span>}
                          </p>
                          {filters.query &&
                            fund.aliases?.some((alias) => alias.includes(filters.query)) && (
                              <small className="muted">曾用名：{fund.aliases[0]}</small>
                            )}
                        </div>
                      </div>
                    </td>
                    <td>{fund.type}</td>
                    <td className="market-number">
                      <strong>{fund.nav}</strong>
                      <small>
                        {fund.chart === 'price' ? '交易价格' : '单位净值'} · {dateOf(fund)}
                      </small>
                    </td>
                    <td
                      className={`market-number ${fund.historical ? 'muted' : fund.change >= 0 ? 'positive' : 'negative'}`}
                    >
                      {fund.historical ? '—' : `${signed(fund.change)}%`}
                    </td>
                    <td>
                      <Link
                        className="market-sector-tag"
                        to={`/sectors/${encodeURIComponent(fund.sector)}?fromFund=${fund.code}`}
                      >
                        {fund.sector}
                      </Link>
                    </td>
                    <td>
                      <Badge tone={coverageTone(fund.coverage)}>
                        {fund.coverage === '完整' ? '基础资料可用' : fund.coverage}
                      </Badge>
                      <small className="market-capability">
                        {fund.historical
                          ? '仅供历史回看'
                          : fund.coverage === '待补全'
                            ? '可搜索 · 趋势不可用'
                            : fund.coverage === '部分资料'
                              ? '部分演示分析可用'
                              : '演示分析可查看'}
                      </small>
                    </td>
                    <td>
                      <Link
                        aria-label={`查看 ${fund.code} 详情`}
                        onClick={rememberScroll}
                        className="market-row-arrow"
                        to={`/funds/${fund.code}?from=${encodeURIComponent(returnPath)}`}
                      >
                        <ArrowUpRight size={18} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="market-pagination">
          <span>
            共 {matching.length} 只基金 · 每页 {pageSize} 条
          </span>
          <div>
            <button
              className="button secondary"
              aria-label="上一页"
              disabled={page <= 1}
              onClick={() => changeFilter('page', String(page - 1))}
            >
              <ChevronLeft size={16} />
            </button>
            <span>
              {page} / {Math.max(1, Math.ceil(matching.length / pageSize))}
            </span>
            <button
              className="button secondary"
              aria-label="下一页"
              disabled={page * pageSize >= matching.length}
              onClick={() => changeFilter('page', String(page + 1))}
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      </Panel>
      <div className="market-bottom-note">
        <Database size={17} />
        <p>
          <strong>了解资料覆盖</strong>
          　基金身份、净值历史与持仓披露各有不同更新周期。“部分资料”仍可浏览，缺口会在详情页逐项说明。
          <Link to="/settings?tab=data">
            查看数据与更新 <ArrowRight size={13} />
          </Link>
        </p>
      </div>
      {adding && (
        <AddFund
          initialCode={/^\d+$/.test(filters.query) ? filters.query : ''}
          onClose={() => setAdding(false)}
        />
      )}
    </div>
  );
}

function PeriodAnalysis({ subject, missing = false }: { subject: string; missing?: boolean }) {
  const { state } = useDemo();
  const judgments = ['暂不操作，观察波动', '关注景气与盈利验证', '持续跟踪配置价值'];
  return (
    <div className="period-grid market-periods">
      {periods.map((period, index) => (
        <section
          className="market-period-card"
          key={period.id}
          style={{ borderTopColor: period.color }}
        >
          <div className="market-period-title">
            <h3>{period.name}</h3>
            <Badge tone={missing ? 'neutral' : index === 0 ? 'neutral' : 'blue'}>
              {missing ? '资料不足' : '本地规则 · 演示'}
            </Badge>
          </div>
          <p className="market-period-range">{period.range}</p>
          <h4>{missing ? '尚不能形成趋势判断' : judgments[index]}</h4>
          <p>
            {missing
              ? '历史净值存在缺口，需要补齐有效观察期。'
              : [
                  '近期波动较高，等待更多量价与公开资料验证。',
                  '结合行业公开资料，观察需求能否延续至盈利。',
                  '关注经营质量与估值变化，定期复核适用条件。',
                ][index]}
          </p>
          <div className="market-risk">
            主要风险：
            {index === 0
              ? '短期反转与交易费用'
              : index === 1
                ? '盈利兑现低于预期'
                : '市场风格与长期假设变化'}
          </div>
          {state.scenario === 'ai-conflict' && (
            <Notice tone="warning">
              AI 候选意见与本地判断冲突，尚未通过校验，不替代本地结果。
            </Notice>
          )}
          <footer>
            <span>依据 {DATA_DATE} · v1.0</span>
            <Link
              to={`/advice?${/^\d{6}$/.test(subject) ? 'fund' : 'sector'}=${encodeURIComponent(subject)}&period=${period.id}`}
            >
              查看{period.name}建议 <ArrowRight size={13} />
            </Link>
          </footer>
        </section>
      ))}
    </div>
  );
}

function FundTrend({ fund }: { fund: MarketFund }) {
  const [kind, setKind] = useState(fund.chart);
  const [range, setRange] = useState(fund.historical ? 'all' : 'quarter');
  const [start, setStart] = useState('2026-08-11');
  const [end, setEnd] = useState(DATA_DATE);
  const fullSeries = useMemo(
    () =>
      fund.historical
        ? makeSeries(false).filter((point) => point.date <= '2025-12-31')
        : makeSeries(fund.coverage !== '完整'),
    [fund.historical, fund.coverage],
  );
  const points = restrictSeries(fullSeries, range, start, end);
  const dateError =
    range === 'custom' &&
    (!start || !end || start > end || start < '2025-09-11' || end > dateOf(fund));
  const option: EChartsOption = {
    animation: false,
    color: ['#427a87'],
    grid: { left: 52, right: 22, top: 22, bottom: 34 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value) =>
        value == null ? '当日资料缺失' : `${Number(value).toFixed(4)} 元`,
    },
    xAxis: {
      type: 'category',
      data: points.map((point) => point.date),
      axisLine: { lineStyle: { color: '#e1e8ed' } },
      axisTick: { show: false },
      axisLabel: {
        color: '#83939d',
        formatter: (value: string) => value.slice(5),
        hideOverlap: true,
      },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitLine: { lineStyle: { color: '#edf1f4', type: 'dashed' } },
      axisLabel: { color: '#83939d' },
    },
    series: [
      {
        name: '单位净值样本（元 / 份）',
        type: 'line',
        data: points.map((point) => point.nav),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.06 },
      },
    ],
  };
  if (kind === 'price')
    return (
      <KlinePanel
        code={fund.code}
        action={
          <Tabs
            items={[
              { label: '交易价格', value: 'price' },
              { label: '单位净值', value: 'nav' },
            ]}
            value={kind}
            onChange={(value) => setKind(value as 'price' | 'nav')}
          />
        }
      />
    );
  return (
    <Panel
      title="单位净值走势 · 演示净值样本"
      subtitle="单位：元 / 份 · 未含分红再投资 · 缺失日期不插值"
      action={
        fund.chart === 'price' ? (
          <Tabs
            items={[
              { label: '交易价格', value: 'price' },
              { label: '单位净值', value: 'nav' },
            ]}
            value={kind}
            onChange={(value) => setKind(value as 'price' | 'nav')}
          />
        ) : (
          <Badge tone="neutral">仅有净值，无交易 K 线</Badge>
        )
      }
    >
      <div className="market-chart-toolbar">
        <Tabs
          value={range}
          onChange={setRange}
          items={[
            { label: '近 1 月', value: 'month' },
            { label: '近 3 月', value: 'quarter' },
            { label: '近 6 月', value: 'half' },
            { label: '近 1 年', value: 'year' },
            { label: '全部', value: 'all' },
            { label: '自定义', value: 'custom' },
          ]}
        />
        <span className="muted">范围仅控制图表</span>
      </div>
      {range === 'custom' && (
        <div className="toolbar market-custom-range">
          <label className="field">
            起始日期
            <input
              type="date"
              min="2025-09-11"
              max={dateOf(fund)}
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </label>
          <label className="field">
            截止日期
            <input
              type="date"
              min="2025-09-11"
              max={dateOf(fund)}
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </label>
          {dateError && (
            <p role="alert" className="market-error">
              请选择资料覆盖内的有效起止日期：2025-09-11 至 {dateOf(fund)}。
            </p>
          )}
        </div>
      )}
      {dateError || !points.length ? (
        <EmptyState
          title="所选范围没有可用资料"
          description="请调整日期范围。历史基金可选择“全部”回看已保存的资料。"
        />
      ) : (
        <>
          <Chart option={option} label="单位净值演示曲线，缺失日期显示断点" height={300} />
          <p className="market-chart-summary">
            已显示 {points[0].date} 至 {points[points.length - 1].date}，共 {points.length}{' '}
            个工作日样本
            {points.some((point) => point.nav == null) && kind === 'nav'
              ? '，其中存在资料缺口，曲线保留断点'
              : ''}
            。图表仅用于检验交互，未计算真实收益。
          </p>
        </>
      )}
    </Panel>
  );
}

function FundExposure({ fund }: { fund: MarketFund }) {
  const [selected, setSelected] = useState('');
  const [ascending, setAscending] = useState(false);
  const [mode, setMode] = useState('industry');
  const stocks = [
    { name: '示例精密科技', code: '600101', sector: '先进制造', weight: 8.6 },
    { name: '示例芯片制造', code: '688101', sector: '半导体', weight: 6.8 },
    { name: '示例智能装备', code: '300101', sector: '先进制造', weight: 5.2 },
    { name: '示例消费零售', code: '600102', sector: '大消费', weight: 4.7 },
  ];
  const industries = [
    { label: '先进制造', value: 13.8 },
    { label: '半导体', value: 6.8 },
    { label: '大消费', value: 4.7 },
    { label: '未披露 / 未识别部分', value: 74.7 },
  ];
  const themes = [
    { label: '科技创新', value: 20.6 },
    { label: '国产替代', value: 18.5 },
    { label: '绿色消费', value: 4.7 },
  ];
  const distribution = mode === 'industry' ? industries : themes;
  const isLink = fund.type === 'ETF联接';
  return (
    <>
      {isLink && (
        <Panel title="ETF 联接关系" subtitle="各层资料日期分别保留，直接持仓与间接暴露分开展示。">
          <div className="market-etf-path">
            <div>
              <Badge tone="blue">本基金</Badge>
              <strong>{fund.name}</strong>
              <small>2026-06-30 · 直接持有目标 ETF 90%</small>
            </div>
            <ArrowRight size={20} />
            <div>
              <Badge tone="blue">目标 ETF</Badge>
              <strong>示例红利低波 ETF</strong>
              <small>披露时点 2026-06-30 · 公开 2026-07-21</small>
            </div>
            <ArrowRight size={20} />
            <div>
              <Badge tone="amber">间接披露</Badge>
              <strong>已穿透 22.77% · 未穿透 77.23%</strong>
              <small>占本基金净资产；不与直接 ETF 比例相加</small>
            </div>
          </div>
          <p className="muted">
            下方股票权重以目标 ETF 净资产为分母；本基金间接暴露为目标 ETF
            比例与相应披露权重的乘积，样例值由演示数据提供。
          </p>
        </Panel>
      )}
      <div className="grid-two market-exposure-grid">
        <Panel
          title={isLink ? '目标 ETF 披露股票' : '披露股票'}
          subtitle={`报告期 ${fund.historical ? '2025-09-30' : '2026-06-30'} · 季报前十大披露中的 4 条演示样本`}
          action={
            <button className="button secondary" onClick={() => setAscending(!ascending)}>
              权重{ascending ? '升序' : '降序'}
              <ArrowDown
                size={13}
                style={{ transform: ascending ? 'rotate(180deg)' : undefined }}
              />
            </button>
          }
        >
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>股票 / 代码</th>
                  <th>披露权重</th>
                  <th>所属行业</th>
                </tr>
              </thead>
              <tbody>
                {[...stocks]
                  .sort((a, b) => (ascending ? a.weight - b.weight : b.weight - a.weight))
                  .map((stock) => (
                    <tr
                      key={stock.code}
                      className={selected === stock.sector ? 'market-highlight' : ''}
                    >
                      <td>
                        <strong>{stock.name}</strong>
                        <small className="market-capability">{stock.code} · 演示身份</small>
                      </td>
                      <td className="market-number">{stock.weight.toFixed(2)}%</td>
                      <td>
                        <Link
                          to={`/sectors/${encodeURIComponent(stock.sector)}?fromFund=${fund.code}`}
                        >
                          {stock.sector}
                        </Link>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <p className="market-chart-summary">
            仅披露样本，不代表实时或完整持仓。未披露部分不视为未持有。
          </p>
        </Panel>
        <Panel
          title="行业与主题暴露"
          subtitle={
            mode === 'industry'
              ? `分母：${isLink ? '目标 ETF' : '本基金'}净资产，未识别部分单列`
              : '主题可能重叠，各项比例不可直接求和'
          }
          action={
            <Tabs
              items={[
                { label: '行业', value: 'industry' },
                { label: '主题', value: 'theme' },
              ]}
              value={mode}
              onChange={(value) => {
                setMode(value);
                setSelected('');
              }}
            />
          }
        >
          <div className="market-distribution">
            {distribution.map((item, index) => (
              <div key={item.label}>
                <div>
                  {mode === 'industry' && index < 3 ? (
                    <button
                      className={selected === item.label ? 'active' : ''}
                      onClick={() => setSelected(selected === item.label ? '' : item.label)}
                    >
                      {item.label}
                    </button>
                  ) : (
                    <span>{item.label}</span>
                  )}
                  <strong>{item.value.toFixed(1)}%</strong>
                </div>
                <div className="market-bar">
                  <span
                    style={{
                      width: `${item.value}%`,
                      background:
                        index === 3 ? '#d8e0e4' : ['#477a85', '#788ac3', '#a5b9a6'][index],
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
          <p className="market-chart-summary">
            {mode === 'industry' ? (
              selected ? (
                <>
                  已高亮“{selected}”对应股票。
                  <Link to={`/sectors/${encodeURIComponent(selected)}?fromFund=${fund.code}`}>
                    进入板块详情
                  </Link>
                </>
              ) : (
                '选择行业可高亮左侧对应股票。归属依据：演示行业分类 v1。'
              )
            ) : (
              '同一股票可归属多个主题；不使用合计为 100% 的主题分布。归属依据：演示主题规则 v1。'
            )}
          </p>
        </Panel>
      </div>
    </>
  );
}

function LegacyFundDetailPage() {
  const { code } = useParams();
  const [params] = useSearchParams();
  const [fees, setFees] = useState(false);
  const fund = marketFunds.find((item) => item.code === code);
  const { state } = useDemo();
  if (!fund)
    return (
      <EmptyState
        title="未找到这只基金"
        description={`演示目录中没有代码 ${code ?? '未知'} 的身份资料，不会自动替换为其他基金。`}
        action={
          <Link className="button primary" to="/funds">
            返回基金库
          </Link>
        }
      />
    );
  const from = params.get('from');
  const back = from === '/funds' || from?.startsWith('/funds?') ? from : '/funds';
  const held = recordedFundCodes(state.transactions).has(fund.code);
  return (
    <div className="market-page">
      <Link className="market-back" to={back}>
        <ChevronLeft size={15} />
        返回基金库
      </Link>
      <PageHeader
        eyebrow="FUND PROFILE"
        title={`${fund.name} · ${fund.share}`}
        description={`${fund.code}　/　${fund.type}　/　${fund.sector}`}
        actions={
          <>
            {!fund.historical && (
              <Link className="button secondary" to={`/advice?fund=${fund.code}`}>
                查看相关建议
              </Link>
            )}
            {!fund.historical && (
              <Link className="button primary" to={`/portfolio?fund=${fund.code}&tab=transactions`}>
                {held ? '查看我的记录' : '录入持仓'}
                <ArrowRight size={16} />
              </Link>
            )}
          </>
        }
      />
      <MarketState />
      {fund.historical && (
        <Notice title="历史基金 · 已停止运作" tone="warning">
          资料截至 2025-12-31，仅供历史回看，不能进行新交易。历史身份与披露记录仍保留。
        </Notice>
      )}
      {fund.coverage !== '完整' && (
        <Notice
          title={
            fund.coverage === '待补全' ? '历史净值不足，趋势分析不可用' : '披露及净值存在局部缺口'
          }
          tone="warning"
        >
          可继续查看已收录的基本资料与披露样本。缺失部分没有补零或推断为完整持仓。
          <Link to={`/settings?tab=data&fund=${fund.code}`}>补全资料</Link>
        </Notice>
      )}
      <div className="market-overview">
        <Stat
          label={fund.chart === 'price' ? '最新交易价格 · 元' : '最新单位净值 · 元 / 份'}
          value={fund.nav}
          detail={`资料反映日期 ${dateOf(fund)}`}
        />
        <Stat
          label="日变化"
          value={fund.historical ? '—' : `${signed(fund.change)}%`}
          detail={
            fund.chart === 'price'
              ? '交易价格相对前日收盘变化 · 演示'
              : '单位净值相对前日变化 · 演示'
          }
        />
        <Stat label="资料覆盖" value={fund.coverage} detail="公开披露不等于实时持仓" />
        <div className="market-fee-summary">
          <span className="muted">费用与适用条件</span>
          <strong>{fund.share} · 费率待核对</strong>
          <button className="market-text-button" onClick={() => setFees(true)}>
            查看费用规则 <ArrowRight size={14} />
          </button>
        </div>
      </div>
      <FundTrend key={fund.code} fund={fund} />
      <FundExposure fund={fund} />
      <div className="market-section-title">
        <div>
          <h2>三个周期，分别判断</h2>
          <p>每个周期有独立依据和风险，图表范围变化不会改变分析周期。</p>
        </div>
        <Badge tone="neutral">分析展示样例</Badge>
      </div>
      {fund.historical ? (
        <Notice title="历史分析快照暂缺">
          该基金仅保存历史身份、净值与披露示例，未保存当时生成的建议。当前分析不会回填为历史结论。
        </Notice>
      ) : (
        <PeriodAnalysis subject={fund.code} missing={fund.coverage === '待补全'} />
      )}
      <Panel title="依据资料与日期">
        <details className="market-evidence">
          <summary>查看来源、保存范围与分类依据</summary>
          <div className="market-evidence-grid">
            <div>
              <span>来源</span>
              <strong>本地虚构演示资料集</strong>
              <p>没有实时抓取或外部行情来源；样本身份和数值不用于投资。</p>
            </div>
            <div>
              <span>净值 / 行情</span>
              <strong>反映 {dateOf(fund)}</strong>
              <p>
                演示公开：当日 20:00
                <br />
                演示获取：当日 20:10，北京时间
              </p>
            </div>
            <div>
              <span>股票披露</span>
              <strong>反映 {fund.historical ? '2025-09-30' : '2026-06-30'}</strong>
              <p>
                演示公开：{fund.historical ? '2025-10-21' : '2026-07-21'}
                <br />
                演示获取：{fund.historical ? '2025-10-22' : '2026-07-22'} 09:00
              </p>
            </div>
            <div>
              <span>规则与分析</span>
              <strong>行业 / 主题分类 v1</strong>
              <p>
                {fund.historical ? '历史分析快照暂缺' : '演示分析：2026-09-12 08:00'}
                <br />
                本地规则 v1.0；未调用 AI
              </p>
            </div>
          </div>
          <p className="muted">
            已保存范围：基金身份、虚构图表序列、4 条股票披露样本
            {fund.historical ? '。历史分析快照暂缺' : '及三周期展示文本'}。真实原始公告尚未接入。
          </p>
        </details>
      </Panel>
      {fees && (
        <Modal title={`${fund.name} ${fund.share} · 费用规则`} onClose={() => setFees(false)}>
          <Notice title="费率尚未接入" tone="warning">
            当前没有可核对的申购、赎回、销售服务费及渠道折扣规则。页面不填入默认费率，也不将费用未知显示为零。
          </Notice>
          <p>
            适用对象：{fund.code} · {fund.share}
            。费用还可能取决于渠道、交易金额与批次持有天数，需由后续业务数据提供。
          </p>
          <Link
            className="button primary"
            to={`/portfolio?fund=${fund.code}&tab=conditions`}
            onClick={() => setFees(false)}
          >
            查看投资条件
          </Link>
        </Modal>
      )}
    </div>
  );
}

function RealFundSeries({
  series,
  code,
  refreshing,
  refreshError,
  onRefresh,
  dateAxis,
}: {
  series: RealFundSeriesResponse | null;
  code: string;
  refreshing: boolean;
  refreshError: string;
  onRefresh: () => void;
  dateAxis: string[];
}) {
  const refreshButton = (
    <button className="button secondary" type="button" onClick={onRefresh} disabled={refreshing}>
      {refreshing ? '正在采集…' : series?.kind && series.rows.length ? '更新数据' : '获取真实数据'}
    </button>
  );
  if (!series || !series.kind || !series.rows.length)
    return (
      <Panel title="真实行情序列" action={refreshButton}>
        <div className="real-catalog-state">
          <p>该基金尚未采集可展示的净值或 K 线序列。</p>
          {refreshError && (
            <p className="notice notice-error" role="alert">
              {refreshError}
            </p>
          )}
        </div>
      </Panel>
    );

  const dates = dateAxis;
  const numberValue = (value: string | null) => (value == null ? null : Number(value));
  const isPrice = series.kind === 'price';
  const priceRows = isPrice ? alignRows(series.rows as RealPriceRow[], dates) : [];
  const navRows = !isPrice ? alignRows(series.rows as RealNavRow[], dates) : [];
  const option: EChartsOption = {
    animation: false,
    legend: { top: 0 },
    grid: isPrice
      ? [
          { left: 58, right: 20, top: 24, height: 220 },
          { left: 58, right: 20, top: 275, height: 60 },
        ]
      : { left: 58, right: 20, top: 24, bottom: 38 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    xAxis: isPrice
      ? [
          { type: 'category', data: dates, boundaryGap: true, axisLabel: { show: false } },
          {
            type: 'category',
            data: dates,
            gridIndex: 1,
            axisLabel: {
              color: '#5d6c80',
              hideOverlap: true,
              formatter: (value: string) => value.slice(5),
            },
          },
        ]
      : {
          type: 'category',
          data: dates,
          axisLabel: {
            color: '#5d6c80',
            hideOverlap: true,
            formatter: (value: string) => value.slice(5),
          },
        },
    yAxis: isPrice
      ? [
          {
            type: 'value',
            scale: true,
            name: '价格',
            axisLabel: { color: '#5d6c80' },
            splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
          },
          {
            type: 'value',
            gridIndex: 1,
            name: '成交量',
            axisLabel: { color: '#5d6c80' },
            splitLine: { show: false },
          },
        ]
      : {
          type: 'value',
          scale: true,
          name: '净值',
          axisLabel: { color: '#5d6c80' },
          splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
        },
    dataZoom: [
      { type: 'inside', xAxisIndex: isPrice ? [0, 1] : [0], start: 0, end: 100 },
      {
        type: 'slider',
        xAxisIndex: isPrice ? [0, 1] : [0],
        bottom: 5,
        height: 20,
        showDetail: false,
      },
    ],
    series: isPrice
      ? [
          {
            name: '真实日 K',
            type: 'candlestick',
            data: priceRows.map((row) => [
              numberValue(row?.open ?? null),
              numberValue(row?.close ?? null),
              numberValue(row?.low ?? null),
              numberValue(row?.high ?? null),
            ]),
          },
          {
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: priceRows.map((row) => numberValue(row?.volume ?? null)),
          },
        ]
      : [
          {
            name: '单位净值',
            type: 'line',
            data: navRows.map((row) => numberValue(row?.unit_nav ?? null)),
            showSymbol: false,
            connectNulls: false,
            lineStyle: { width: 2 },
          },
          {
            name: '累计净值',
            type: 'line',
            data: navRows.map((row) =>
              numberValue(row?.accumulated_nav ?? row?.cumulative_nav ?? null),
            ),
            showSymbol: false,
            connectNulls: false,
            lineStyle: { width: 2, type: 'dashed' },
          },
        ],
  };
  return (
    <Panel
      title={isPrice ? '真实 K 线' : '真实净值走势'}
      action={refreshButton}
      subtitle={`行情截至 ${series.rows.at(-1)?.date} · ${series.source_id || '来源待补充'}${series.updated_at ? ` · 入库于 ${series.updated_at.slice(0, 10)}` : ''}`}
    >
      {refreshError && (
        <div className="notice notice-error" role="alert">
          {refreshError}
        </div>
      )}
      <p className="muted">
        源数据区间 {series.rows[0].date} 至 {series.rows.at(-1)?.date} · {series.rows.length} 条记录
        · 口径 {series.policy_version || '待提供'}。价格或净值曲线不直接等于含分红、费用的投资收益。
      </p>
      <Chart
        option={option}
        label={`${code}${isPrice ? '真实K线' : '真实净值走势'}`}
        height={390}
        linkGroup={`fund-market-${code}`}
      />
    </Panel>
  );
}

function RelatedMarketPanel({
  market,
  dateAxis,
  fundCode,
}: {
  market: RelatedMarket | null | undefined;
  dateAxis: string[];
  fundCode: string;
}) {
  if (!market) return null;
  if (market.relation_status !== 'linked')
    return (
      <Panel title="关联待核验">
        <p>{market.relation_reason || '关联来源或核验状态不足，不绘制默认指数。'}</p>
      </Panel>
    );
  if (!market.rows.length)
    return (
      <Panel title="关联指数行情">
        <p>关系已记录，但该指数尚无可用日线。</p>
      </Panel>
    );
  const numberValue = (value: string | null) => (value == null ? null : Number(value));
  const dates = dateAxis;
  const aligned = alignRows(market.rows, dates);
  const option: EChartsOption = {
    animation: false,
    legend: { top: 0 },
    grid: { left: 58, right: 20, top: 24, bottom: 38 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: true,
      axisLabel: {
        color: '#5d6c80',
        hideOverlap: true,
        formatter: (value: string) => value.slice(5),
      },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: '指数价格',
      axisLabel: { color: '#5d6c80' },
      splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 5, height: 20 },
    ],
    series: [
      {
        name: '真实指数 K 线',
        type: 'candlestick',
        data: aligned.map((row) => [
          numberValue(row?.open ?? null),
          numberValue(row?.close ?? null),
          numberValue(row?.low ?? null),
          numberValue(row?.high ?? null),
        ]),
      },
    ],
  };
  return (
    <Panel
      title="关联指数行情"
      subtitle={`${market.name} · ${market.code} · ${market.source_id}`}
      action={
        <Link
          className="button secondary"
          to={researchHref({
            code: market.code,
            source_id: market.source_id,
            universe_type: 'tracked_index',
          })}
        >
          查看三周期研究
        </Link>
      }
    >
      <p className="muted">
        行情截至 {market.rows.at(-1)?.date} · 关系核验于 {market.verified_at || '未提供'} · 关系来源{' '}
        {market.relation_source_id || '未提供'}{' '}
        {sourceHref(market.evidence_url) && (
          <a href={sourceHref(market.evidence_url)!} target="_blank" rel="noreferrer">
            核对证据
          </a>
        )}
      </p>
      <p className="muted">已存储的基金—指数关系不等于板块投资推荐；指数表现不等于基金实际回报。</p>
      <Chart
        option={option}
        label={`${market.code}关联板块真实K线`}
        height={340}
        linkGroup={`fund-market-${fundCode}`}
      />
    </Panel>
  );
}

export function FundsPage() {
  return (
    <div className="market-page">
      <PageHeader
        eyebrow="MARKET EXPLORER"
        title="基金库"
        description="查询已收录的真实基金身份信息。"
      />
      <MarketNavigation selected="funds" />
      <RealFundDirectory />
    </div>
  );
}

export function FundDetailPage() {
  const { code = '' } = useParams();
  // A route change creates a new state owner; an old refresh cannot replace a new fund.
  return <RealFundDetail key={code} />;
}

function RealFundDetail() {
  const { code = '' } = useParams();
  const [params] = useSearchParams();
  const [fund, setFund] = useState<RealFund | null>(null);
  const [series, setSeries] = useState<RealFundSeriesResponse | null>(null);
  const [relatedMarket, setRelatedMarket] = useState<RelatedMarket | null>(null);
  const [relatedMarkets, setRelatedMarkets] = useState<RelatedMarket[]>([]);
  const [seriesOptions, setSeriesOptions] = useState<RealFundSeriesResponse[]>([]);
  const [seriesKind, setSeriesKind] = useState<'price' | 'nav' | null>(null);
  const [chartRange, setChartRange] = useState('all');
  const [chartStart, setChartStart] = useState('');
  const [chartEnd, setChartEnd] = useState('');
  const selectedSeries = seriesOptions.find((option) => option.kind === seriesKind) ?? series;
  const dates = useMemo(
    () =>
      commonDates([
        selectedSeries?.rows ?? [],
        relatedMarket?.relation_status === 'linked' ? relatedMarket.rows : [],
      ]),
    [selectedSeries, relatedMarket],
  );
  const dateAxis = useMemo(
    () => rangeDates(dates, chartRange, chartStart, chartEnd),
    [dates, chartRange, chartStart, chartEnd],
  );
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'missing'>('loading');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/funds/${encodeURIComponent(code)}`, { signal: controller.signal })
      .then(async (response) => {
        if (response.status === 404) {
          setStatus('missing');
          return null;
        }
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then((data: RealFundDetailResponse | null) => {
        if (data?.fund) {
          if (data.fund.code !== code) throw new Error('基金身份与请求不一致');
          setFund(data.fund);
          setSeries(data.series ?? null);
          setRelatedMarket(data.related_market ?? null);
          setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
          setRelatedMarkets(data.related_markets ?? []);
          setStatus('ready');
        } else setStatus('missing');
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setStatus('error');
      });
    return () => controller.abort();
  }, [code]);
  const refresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError('');
    fetch(`/api/funds/${encodeURIComponent(code)}/refresh`, { method: 'POST' })
      .then(async (response) => {
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          const current = data?.error?.details?.current as RealFundDetailResponse | undefined;
          if (current?.fund?.code === code) {
            setSeries(current.series ?? null);
            setSeriesOptions(
              current.series_options ?? (current.series?.kind ? [current.series] : []),
            );
            setRelatedMarket(current.related_market ?? null);
            setRelatedMarkets(current.related_markets ?? []);
          }
          throw new Error(data?.error?.message || '真实数据采集失败，请稍后重试。');
        }
        if (!data?.fund || data.fund.code !== code)
          throw new Error('返回的基金身份与请求不一致，旧数据已保留。');
        return data as RealFundDetailResponse;
      })
      .then((data) => {
        setFund(data.fund);
        setSeries(data.series ?? null);
        setRelatedMarket(data.related_market ?? null);
        setRelatedMarkets(data.related_markets ?? []);
        setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
        if (data.refresh?.status === 'partial') {
          setRefreshError(
            '本次部分更新成功。' +
              Object.entries(data.refresh.stages)
                .map(
                  ([name, stage]) =>
                    `${name === 'fund_series' ? '基金序列' : '关联指数'}：${stage.message}`,
                )
                .join('；'),
          );
        }
      })
      .catch((cause: unknown) => {
        if (cause instanceof TypeError) {
          setRefreshError('本地数据服务未连接。请重新启动本地网页后再试。');
          return;
        }
        setRefreshError(cause instanceof Error ? cause.message : '真实数据采集失败，请稍后重试。');
      })
      .finally(() => setRefreshing(false));
  };
  const back = safeReturnPath(params.get('from'), '/funds');
  if (status === 'loading')
    return (
      <div className="market-page">
        <div className="real-catalog-state" role="status">
          正在读取真实基金身份…
        </div>
      </div>
    );
  if (status === 'error')
    return (
      <div className="market-page">
        <EmptyState
          title="真实基金身份暂时无法加载"
          description="请确认本地数据服务已启动后重试。"
          action={
            <Link className="button primary" to={back}>
              返回基金库
            </Link>
          }
        />
      </div>
    );
  if (status === 'missing' || !fund)
    return (
      <div className="market-page">
        <EmptyState
          title="未找到这只基金"
          description={`本地真实目录中没有代码 ${code} 的身份资料。`}
          action={
            <Link className="button primary" to={back}>
              返回基金库
            </Link>
          }
        />
      </div>
    );
  return (
    <div className="market-page">
      <Link className="market-back" to={back}>
        <ChevronLeft size={15} />
        {back.startsWith('/advice') ? '返回研究页' : '返回基金库'}
      </Link>
      <PageHeader
        eyebrow="FUND PROFILE"
        title={fund.name}
        description={`${fund.code} / ${fund.fund_type || '基金类型待补充'}`}
        actions={
          <Link
            className="button primary"
            to={`/advice?fund=${fund.code}&from=${encodeURIComponent(`/funds/${fund.code}`)}`}
          >
            查看该基金的关联研究
          </Link>
        }
      />
      <Panel title="基金身份">
        <dl className="market-overview">
          <div>
            <dt>基金代码</dt>
            <dd>{fund.code}</dd>
          </div>
          <div>
            <dt>份额 ID</dt>
            <dd>{fund.share_id}</dd>
          </div>
          <div>
            <dt>基金类型</dt>
            <dd>{fund.fund_type || '—'}</dd>
          </div>
          <div>
            <dt>数据来源</dt>
            <dd>{fund.source_id || '—'}</dd>
          </div>
        </dl>
      </Panel>
      {seriesOptions.length > 1 && (
        <Tabs
          value={selectedSeries?.kind ?? 'nav'}
          onChange={(value) => setSeriesKind(value as 'price' | 'nav')}
          items={seriesOptions.map((option) => ({
            value: option.kind!,
            label: option.kind === 'price' ? '交易价格' : '基金净值',
          }))}
        />
      )}
      {!!dates.length && (
        <Panel
          title="共同查看区间"
          subtitle="同步日期与缩放，不等于同口径收益对比；未接入交易日历，无法识别双方共同缺失的交易日。"
        >
          <Tabs
            value={chartRange}
            onChange={setChartRange}
            items={[
              { value: 'month', label: '近 1 月' },
              { value: 'quarter', label: '近 3 月' },
              { value: 'half', label: '近 6 月' },
              { value: 'year', label: '近 1 年' },
              { value: 'all', label: '全部' },
              { value: 'custom', label: '自定义' },
            ]}
          />
          {chartRange === 'custom' && (
            <div className="toolbar">
              <label>
                起始日期
                <input
                  aria-label="图表起始日期"
                  type="date"
                  min={dates[0]}
                  max={dates.at(-1)}
                  value={chartStart}
                  onChange={(event) => setChartStart(event.target.value)}
                />
              </label>
              <label>
                截止日期
                <input
                  aria-label="图表截止日期"
                  type="date"
                  min={dates[0]}
                  max={dates.at(-1)}
                  value={chartEnd}
                  onChange={(event) => setChartEnd(event.target.value)}
                />
              </label>
            </div>
          )}
          {!dateAxis.length && <p role="alert">请选择有效且有数据的日期区间。</p>}
        </Panel>
      )}
      <RealFundSeries
        code={fund.code}
        series={selectedSeries}
        refreshing={refreshing}
        refreshError={refreshError}
        onRefresh={refresh}
        dateAxis={dateAxis}
      />
      <RelatedMarketPanel market={relatedMarket} dateAxis={dateAxis} fundCode={fund.code} />
      {relatedMarkets.some((relation) => relation.relation_status !== 'linked') && (
        <Panel title="历史关联与核验缺项">
          {relatedMarkets
            .filter((relation) => relation.relation_status !== 'linked')
            .map((relation) => (
              <p key={`${relation.code}-${relation.relation_status}`}>
                {relation.name} · {relation.code}：
                {relation.relation_reason || '尚未通过当前核验。'}
              </p>
            ))}
        </Panel>
      )}
      {!relatedMarket && (
        <Panel title="对应指数 / 板块">
          <p className="muted">
            尚无已核验的对应关系，不按基金名称猜测行业，也不套用其他基金的板块资料。
          </p>
        </Panel>
      )}
      <Panel title="研究与基金推荐的资料边界">
        <p>
          真实身份和走势已展示；基金披露持仓、行业穿透、完整费用、跟踪表现及同类择优尚未接入此页。
        </p>
        <p className="muted">
          缺项不表示零持仓、零费用或不值得投资。关联研究只展示当前可核验内容，不生成买入金额。
        </p>
      </Panel>
    </div>
  );
}

export function SectorsPage() {
  return (
    <div className="market-page">
      <PageHeader
        eyebrow="SECTOR OPPORTUNITIES"
        title="板块机会"
        description="结合行业经营、估值线索与真实走势，观察三个周期的支持证据和待确认条件。"
      />
      <MarketNavigation selected="sectors" />
      <SectorOpportunities />
    </div>
  );
}

export function SectorDetailPage() {
  const [params] = useSearchParams();
  return params.has('source_id') || params.has('universe_type') ? <RealSectorDetail /> : <DemoSectorDetailPage />;
}

function DemoSectorDetailPage() {
  const { sector: name } = useParams();
  const [params] = useSearchParams();
  const [trendRange, setTrendRange] = useState('quarter');
  const { state } = useDemo();
  const sector = sectorDefinitions.find((item) => item.name === name);
  if (!sector)
    return (
      <EmptyState
        title="未找到该板块"
        description="当前演示分类没有这一条目，已保留原链接而未替换其他板块。"
        action={
          <Link className="button primary" to="/sectors">
            返回板块机会
          </Link>
        }
      />
    );
  const sourceFund = marketFunds.find((fund) => fund.code === params.get('fromFund'));
  const related = marketFunds.filter((fund) => fund.sector === sector.name && !fund.historical);
  const held = recordedFundCodes(state.transactions);
  const trendPoints = restrictSeries(makeSeries(), trendRange, '', '');
  const trendOption: EChartsOption = {
    animation: false,
    grid: { left: 54, right: 22, top: 20, bottom: 30 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value) => `${Number(value).toFixed(2)} 点（演示）`,
    },
    xAxis: {
      type: 'category',
      data: trendPoints.map((point) => point.date),
      axisLabel: {
        color: '#687987',
        formatter: (value) => String(value).slice(5),
        hideOverlap: true,
      },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: '#e3e9ed' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: '#687987' },
      splitLine: { lineStyle: { color: '#e9eff2', type: 'dashed' } },
    },
    series: [
      {
        type: 'line',
        name: '虚构指数样本',
        data: trendPoints.map((point) => Number(((point.nav ?? 0) * 1000).toFixed(2))),
        showSymbol: false,
        itemStyle: { color: sector.color },
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.05 },
      },
    ],
  };
  return (
    <div className="market-page">
      <Link className="market-back" to="/sectors">
        <ChevronLeft size={15} />
        返回板块机会
      </Link>
      <PageHeader
        eyebrow="SECTOR PROFILE"
        title={sector.name}
        description={`${sector.kind}　/　演示分类 v1　/　资料反映日期 ${DATA_DATE}`}
        actions={
          <Link className="button primary" to={`/advice?sector=${encodeURIComponent(sector.name)}`}>
            查看三个周期建议
            <ArrowRight size={16} />
          </Link>
        }
      />
      <MarketState />
      {sourceFund && (
        <Notice title={`来自基金：${sourceFund.name} ${sourceFund.share}`}>
          当前查看与 {sourceFund.code} 的投资关联，以下“相关基金”尚未经过个人条件适配。
          <Link to={`/funds/${sourceFund.code}`}>返回来源基金</Link>
        </Notice>
      )}
      <div className="grid-two">
        <Panel title="板块定义与观察线索">
          <Badge tone="blue">{sector.kind}分类 · 演示依据</Badge>
          <h3 className="market-sector-lead">{sector.summary}</h3>
          <p className="muted">
            {sector.kind === '行业'
              ? '按照主要经营活动划分。相关基金来自已披露股票的行业归属，并保留未知部分。'
              : '按照共同投资特征划分。同一股票可能命中其他主题，各主题暴露存在重叠。'}
          </p>
          <div className="market-risk">主要风险：{sector.risk}</div>
        </Panel>
        <Panel title="可用指标" subtitle="以下指标用于界面展示，均为虚构样本">
          <div className="market-sector-stats">
            <Stat
              label="指数日变化"
              value={`${signed(sector.change)}%`}
              detail={`演示指数 · ${DATA_DATE}`}
            />
            <Stat
              label="相关演示基金"
              value={String(related.length)}
              detail="按照披露与主题规则匹配"
            />
          </div>
          <p className="muted">
            行业盈利趋势、估值分位与资金流向：尚未接入，因此不提供数值或综合评分。
          </p>
        </Panel>
      </div>
      <Panel
        title="板块指数走势 · 演示样本"
        subtitle="单位：指数点位 · 不含分红再投资 · 独立于三周期分析"
        action={
          <Tabs
            value={trendRange}
            onChange={setTrendRange}
            items={[
              { label: '近 1 月', value: 'month' },
              { label: '近 3 月', value: 'quarter' },
              { label: '近 6 月', value: 'half' },
            ]}
          />
        }
      >
        {sector.name === '清洁能源' ? (
          <EmptyState
            title="板块趋势资料尚未收录"
            description="目前只有分类和基础身份资料，未绘制推算曲线。"
            action={
              <Link className="button secondary" to="/settings?tab=data">
                查看资料缺口
              </Link>
            }
          />
        ) : (
          <>
            <Chart
              option={trendOption}
              label={`${sector.name}虚构板块指数趋势，仅用于界面审查`}
              height={250}
            />
            <p className="market-chart-summary">
              显示 {trendPoints[0].date} 至 {trendPoints[trendPoints.length - 1].date}，共{' '}
              {trendPoints.length} 个工作日样本。真实行情与收益尚未接入。
            </p>
          </>
        )}
      </Panel>
      <div className="market-section-title">
        <div>
          <h2>三周期分析</h2>
          <p>各周期分别判断，当前没有自动合并的操作方案。</p>
        </div>
      </div>
      <PeriodAnalysis
        subject={sector.name}
        missing={sector.name === '清洁能源' || sector.name === '医药健康'}
      />
      <Panel
        title="相关基金"
        subtitle="仅表示与板块相关；尚未结合你的资金、持仓与风险条件生成个性化推荐。"
      >
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>基金名称 / 份额</th>
                <th>匹配依据 / 暴露分母</th>
                <th>费用与可交易条件</th>
                <th>持仓状态</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {related.map((fund) => (
                <tr key={fund.code}>
                  <td>
                    <Link className="market-fund-title" to={`/funds/${fund.code}`}>
                      {fund.name}
                      <span className="market-share">{fund.share}</span>
                    </Link>
                    <small className="market-capability">
                      {fund.code} · {fund.type}
                    </small>
                  </td>
                  <td>
                    {fund.type === 'ETF联接' ? '经目标 ETF 间接匹配' : '主要投资方向匹配'}
                    <small className="market-capability">
                      具体基金暴露待完整披露核对，不推断全仓
                    </small>
                  </td>
                  <td>
                    费率 / 交易限制待接入
                    <small className="market-capability">需按渠道与份额确认</small>
                  </td>
                  <td>
                    <Badge tone={held.has(fund.code) ? 'blue' : 'neutral'}>
                      {held.has(fund.code) ? '有我的记录' : '无我的记录'}
                    </Badge>
                  </td>
                  <td>
                    <Link aria-label={`查看 ${fund.code} 详情`} to={`/funds/${fund.code}`}>
                      <ArrowUpRight size={18} />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="支撑资料">
        <details className="market-evidence">
          <summary>查看分类、来源与分析时点</summary>
          <p>
            来源：本地虚构演示资料；分类版本 v1。股票持仓反映日期 2026-06-30，演示公开日期
            2026-07-21，演示获取时间 2026-07-22 09:00（北京时间）。
          </p>
          <p>
            分析展示生成时点：2026-09-12 08:00；策略 v1.0。未调用
            AI，真实来源公告与行业研究资料尚未接入。
          </p>
        </details>
      </Panel>
    </div>
  );
}
