import { useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { EmptyState, Notice, PageHeader } from '../../shared/ui';
import { safeReturnPath } from '../advice/research-model';
import { OpportunityCard } from './SectorOpportunities';
import { parseSectorDetail } from './sector-detail-model';
import { MarketDataRefresh } from './MarketDataRefresh';

type Detail = ReturnType<typeof parseSectorDetail>;
export function RealSectorDetail() {
  const { sector: code = '' } = useParams();
  const [params] = useSearchParams();
  const source = params.get('source_id') ?? '';
  const universe = params.get('universe_type') ?? '';
  const key = JSON.stringify([code, source, universe]);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ key: string; data: Detail | null; error: string }>({ key, data: null, error: '' });
  useEffect(() => {
    const controller = new AbortController();
    setState({ key, data: null, error: '' });
    if (!code || !source || (universe !== 'hot_board' && universe !== 'tracked_index')) {
      setState({ key, data: null, error: '真实板块链接缺少有效来源或研究范围，未回退到演示板块。' });
      return () => controller.abort();
    }
    const query = new URLSearchParams({ source_id: source, universe_type: universe });
    void fetch(`/api/sectors/${encodeURIComponent(code)}?${query}`, { signal: controller.signal }).then(async (response) => {
      const raw = await response.json().catch(() => null);
      if (!response.ok) throw new Error(raw?.error?.message || '未取得该来源下的真实板块详情。');
      return parseSectorDetail(raw, { code, source_id: source, universe_type: universe });
    }).then((data) => {
      if (!controller.signal.aborted) setState({ key, data, error: '' });
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setState({ key, data: null, error: cause instanceof Error ? cause.message : '读取真实板块失败。' });
    });
    return () => controller.abort();
  }, [code, source, universe, key, attempt]);
  const visible = state.key === key ? state : { key, data: null, error: '' };
  const fromFund = params.get('fromFund') ?? '';
  return <div className="market-page">
    <Link className="market-back" to={safeReturnPath(params.get('from'), '/sectors')}>返回来源列表</Link>
    {visible.error ? <EmptyState title="真实板块暂不可用" description={visible.error} action={<button className="button secondary" onClick={() => setAttempt((value) => value + 1)}>重试详情</button>} /> : !visible.data ? <p role="status">正在读取真实板块详情…</p> : <>
      <PageHeader eyebrow="REAL SECTOR PROFILE" title={visible.data.item.name} description={`${code} · ${source} · ${universe === 'tracked_index' ? '参考指数' : '行业板块'} · 评估时间 ${visible.data.generated_at}`} />
      <Notice title="真实行情与研究证据">基金跟踪关系不等于行业持仓穿透，也不代表投资推荐。经营、估值和催化资料不足时保持缺项。</Notice>
      {/^[0-9]{6}$/.test(fromFund) && <div>
        <Link to={`/funds/${fromFund}`}>返回来源基金 {fromFund}</Link>
        {!visible.data.item.funds.some((fund) => fund.code === fromFund && fund.relation_status === 'linked') &&
          <p role="alert">来源基金与本板块的当前关系未通过核验；保留导航上下文，不再视为有效关联。</p>}
      </div>}
      {universe === 'hot_board' && <MarketDataRefresh target="sectors" onSettled={() => setAttempt((value) => value + 1)} />}
      <OpportunityCard item={visible.data.item} />
    </>}
  </div>;
}
