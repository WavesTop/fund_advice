import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { Panel } from '../../shared/ui';
import { directoryLayout, directoryPage, parseDirectory, type DirectoryResult } from './fund-directory-model';
import { MarketDataRefresh } from './MarketDataRefresh';
import { sectorDetailHref } from './sector-detail-model';
import './fund-directory.css';

const historyKey = 'fundAdvicer.realFundSearchHistory';
function readHistory(): string[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(historyKey) || 'null');
    return Array.isArray(raw) ? [...new Set(raw.filter((value): value is string => typeof value === 'string').map((value) => value.trim()).filter(Boolean))].slice(0, 8) : [];
  } catch { return []; }
}
interface State { key: string; result: DirectoryResult | null; loading: boolean; error: string }

export function FundDirectory() {
  const [params, setParams] = useSearchParams();
  const submittedQuery = (params.get('q') ?? '').trim();
  const page = directoryPage(params.get('page'));
  const [layout, setLayout] = useState(() => directoryLayout(params.get('page_size') === '8' ? 1200 : 900));
  const pageSize = layout.pageSize;
  const measuredSize = useRef(pageSize);
  const container = useRef<HTMLDivElement>(null);
  const requestKey = JSON.stringify([submittedQuery, page, pageSize]);
  const [query, setQuery] = useState(submittedQuery);
  const [attempt, setAttempt] = useState(0);
  const [history, setHistory] = useState(readHistory);
  const [state, setState] = useState<State>({ key: requestKey, result: null, loading: true, error: '' });

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const measure = (width: number) => {
      if (width <= 0) return;
      const nextLayout = directoryLayout(width);
      setLayout((old) => old.columns === nextLayout.columns ? old : nextLayout);
      if (measuredSize.current !== nextLayout.pageSize) {
        measuredSize.current = nextLayout.pageSize;
        setParams((old) => {
          const next = new URLSearchParams(old);
          next.delete('page');
          next.set('page_size', String(nextLayout.pageSize));
          return next;
        }, { replace: true });
      }
    };
    measure(element.getBoundingClientRect().width);
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver((entries) => measure(entries[0]?.contentRect.width ?? 0));
    observer.observe(element);
    return () => observer.disconnect();
  }, [setParams]);
  useEffect(() => setQuery(submittedQuery), [submittedQuery]);
  useEffect(() => {
    const controller = new AbortController();
    setState((old) => ({ key: requestKey, result: old.key === requestKey ? old.result : null, loading: true, error: '' }));
    const search = new URLSearchParams({ q: submittedQuery, page: String(page), page_size: String(pageSize) });
    void fetch(`/api/funds?${search}`, { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error(`本地基金目录读取失败（HTTP ${response.status}）。`);
      const result = parseDirectory(await response.json());
      if (result.page !== page || result.page_size !== pageSize || result.items.length > pageSize) throw new Error('返回分页与本次请求不一致，未展示错页数据。');
      return result;
    }).then((result) => {
      if (controller.signal.aborted) return;
      setState({ key: requestKey, result, loading: false, error: '' });
      if (submittedQuery) setHistory((old) => {
        const next = [submittedQuery, ...old.filter((entry) => entry !== submittedQuery)].slice(0, 8);
        try { localStorage.setItem(historyKey, JSON.stringify(next)); } catch { /* Optional browser history only. */ }
        return next;
      });
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setState((old) => ({ key: requestKey, result: old.key === requestKey ? old.result : null, loading: false,
        error: cause instanceof Error ? cause.message : '本地真实目录暂不可用，请重试。' }));
    });
    return () => controller.abort();
  }, [requestKey, submittedQuery, page, pageSize, attempt]);

  const visible = state.key === requestKey ? state : { key: requestKey, result: null, loading: true, error: '' };
  const result = visible.result;
  const totalPages = Math.max(1, Math.ceil((result?.total ?? 0) / pageSize));
  const hasSearch = Boolean(submittedQuery);
  const navigate = (value: string, nextPage = 1) => {
    const next = new URLSearchParams(params);
    if (value.trim()) next.set('q', value.trim()); else next.delete('q');
    if (nextPage > 1) next.set('page', String(nextPage)); else next.delete('page');
    next.set('page_size', String(pageSize));
    setParams(next);
    if (value.trim() === submittedQuery && nextPage === page) setAttempt((old) => old + 1);
  };
  const returnPath = `/funds${params.toString() ? `?${params}` : ''}`;

  return <div ref={container} className="fund-directory-layout"><Panel className="real-fund-catalog">
    <div className="market-list-heading"><div><h2>本地真实基金目录</h2>
      <p className="muted">已收录 {result?.catalog_total ?? '—'} 条基金份额；查询身份、真实走势和关联研究。目录收录不代表具备推荐能力。</p>
    </div><span className="market-date">{result?.updated_at ? `目录更新于 ${result.updated_at.slice(0, 10)}` : '本地数据服务'}</span></div>
    <div className="fund-directory-refresh"><MarketDataRefresh target="catalog" onSettled={() => setAttempt((value) => value + 1)} /></div>
    {result?.collection && <div className="catalog-progress" aria-label="真实数据采集进度"><span>已采集 <strong>{result.collection.collected}</strong></span><span>失败 <strong>{result.collection.failed}</strong></span><span>待采集 <strong>{result.collection.pending}</strong></span></div>}
    <form className="market-search-row" onSubmit={(event) => { event.preventDefault(); navigate(query); }}>
      <label className="market-search"><Search size={18} aria-hidden="true" /><input type="search" aria-label="搜索本地真实基金" placeholder="输入基金名称或 6 位代码" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <button className="button primary" type="submit">搜索</button>
    </form>
    {history.length > 0 && <div className="catalog-search-history" aria-label="本地搜索历史"><span className="muted">最近搜索</span>
      {history.map((entry) => <button key={entry} className="button secondary" aria-pressed={entry === submittedQuery} onClick={() => { setQuery(entry); navigate(entry); }}>{entry}</button>)}
      <button className="button-link" onClick={() => { setHistory([]); try { localStorage.removeItem(historyKey); } catch { /* Optional history. */ } }}>清除历史</button></div>}
    {visible.loading && <div className="real-catalog-state" role="status">正在读取本地真实目录…</div>}
    {visible.error && <div className="real-catalog-state" role="alert"><p>{visible.error} 请确认本地数据服务已启动。</p>{result && <p>当前保留本次查询上次成功的结果；不是刚更新的目录。</p>}<button className="button secondary" disabled={visible.loading} onClick={() => setAttempt((value) => value + 1)}>重试</button></div>}
    {!hasSearch && result && <div className="real-catalog-state"><p>请输入关键词开始查找。</p><p className="muted">搜索名称或代码后可以查看真实基金卡片；不默认展示推荐基金。</p><div className="catalog-examples" aria-label="搜索案例">{['510050', '005911', '成长', '指数'].map((example) => <button className="button secondary" key={example} onClick={() => { setQuery(example); navigate(example); }}>搜索“{example}”</button>)}</div></div>}
    {hasSearch && result && !result.items.length && <div className="real-catalog-state">没有找到“{submittedQuery}”对应的基金（当前查询页）。{page > 1 && <button className="button secondary" onClick={() => navigate(submittedQuery)}>返回查询第一页</button>}</div>}
    {hasSearch && result && result.items.length > 0 && <>
      <h3 className="fund-results-heading">“{submittedQuery}”的基金搜索结果</h3>
      <ul className="fund-card-grid" aria-label="基金搜索结果" aria-busy={visible.loading} style={{ gridTemplateColumns: `repeat(${layout.columns}, minmax(0, 1fr))` }}>
        {result.items.map((fund) => <li key={fund.share_id} className="fund-search-card">
          <article aria-label={`${fund.name} ${fund.code}`}>
            <h4><Link className="market-fund-title" to={`/funds/${fund.code}?from=${encodeURIComponent(returnPath)}`}>{fund.name}</Link></h4>
            <p className="fund-card-identity">{fund.code} · {fund.fund_type || '类型待提供'}</p>
            <small className="market-fund-meta">份额 ID：{fund.share_id} · 来源：{fund.source_id || '待提供'}</small>
            <div className="fund-card-sectors"><strong>关联板块／指数</strong>
              {fund.relation_status === 'linked' && fund.related_sectors?.length === 1 ? fund.related_sectors.map((sector) => <div key={`${sector.source_id}:${sector.code}`}>
                <Link to={sectorDetailHref(sector, returnPath, fund.code)}>查看{sector.name}详情</Link>
                <small className="market-fund-meta">关系来源：{sector.relation_source_id} · 核验于 {sector.verified_at.slice(0, 10)}</small>
              </div>) : <p className="muted">{fund.relation_reason || '尚无已核验板块关联；不按基金名称推断。'}</p>}
            </div>
            <Link to={`/advice?fund=${fund.code}&from=${encodeURIComponent(returnPath)}`}>查看关联研究</Link>
          </article>
        </li>)}
      </ul>
    </>}
    {hasSearch && result && <nav className="market-pagination" aria-label="基金搜索分页"><span>搜索到 {result.total} 条 · 第 {result.page} / {totalPages} 页 · 每页 {pageSize} 张卡片</span><div><button className="button secondary" aria-label="本地目录上一页" disabled={page <= 1 || visible.loading} onClick={() => navigate(submittedQuery, page - 1)}><ChevronLeft size={16} />上一页</button><button className="button secondary" aria-label="本地目录下一页" disabled={page >= totalPages || visible.loading} onClick={() => navigate(submittedQuery, page + 1)}>下一页<ChevronRight size={16} /></button></div></nav>}
  </Panel></div>;
}
