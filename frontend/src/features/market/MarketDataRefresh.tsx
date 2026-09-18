import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import type { OpportunitiesResponse } from './SectorOpportunities';
import { pendingRefresh, startSharedRefresh, subscribeRefresh,
  type RefreshTarget, type RefreshResult } from './sector-request-state';

type Outcome = { status: 'success' | 'partial' | 'failed'; message: string };
interface Props {
  target: RefreshTarget;
  onSettled: (evaluation?: OpportunitiesResponse) => void;
  onStart?: () => void;
}

/** Changing routes unsubscribes the view, not the server-side collection. */
export function MarketDataRefresh({ target, onSettled, onStart }: Props) {
  const [busy, setBusy] = useState(Boolean(pendingRefresh(target)));
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const callback = useRef({ onSettled, onStart });
  callback.current = { onSettled, onStart };
  useEffect(() => {
    let mounted = true;
    let watched: Promise<RefreshResult> | undefined;
    const observe = () => {
      const task = pendingRefresh(target);
      setBusy(Boolean(task));
      if (!task || task === watched) return;
      watched = task;
      setOutcome(null);
      callback.current.onStart?.();
      void task.then((result) => {
        if (!mounted) return;
        setOutcome(result);
        callback.current.onSettled(result.evaluation);
      }, (cause: unknown) => {
        if (!mounted) return;
        setOutcome({ status: 'failed', message: cause instanceof Error ? cause.message : '联网采集失败。' });
        callback.current.onSettled();
      });
    };
    const unsubscribe = subscribeRefresh(observe);
    observe();
    return () => { mounted = false; unsubscribe(); };
  }, [target]);

  return <div className="market-data-refresh">
    <button className="button secondary" type="button" disabled={busy}
      onClick={() => { startSharedRefresh(target); }}>
      <RefreshCw size={16} aria-hidden="true" />
      {busy ? target === 'sectors' ? '正在获取最新数据并评估…' : '正在联网采集…'
        : target === 'catalog' ? '更新基金目录' : '重新评估'}
    </button>
    {busy && <p role="status">正在访问公开数据源，原有资料保留；切换页面后继续复用本次任务，不重复采集。</p>}
    {outcome && <p role={outcome.status === 'success' ? 'status' : 'alert'}>
      <strong>{outcome.status === 'success' ? target === 'sectors' ? '行情更新及评估完成：' : '更新完成：'
        : outcome.status === 'partial' ? '部分更新完成：' : '未完成更新：'}</strong>{outcome.message}
    </p>}
  </div>;
}
