import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { parseSectorEvaluation } from './sector-evaluation-model';
import type { OpportunitiesResponse } from './SectorOpportunities';

type Target = 'catalog' | 'sectors';
type Outcome = { status: 'success' | 'partial' | 'failed'; message: string };
const endpoints: Record<Target, string> = {
  catalog: '/api/funds/catalog/refresh',
  sectors: '/api/sectors/refresh',
};
interface Props {
  target: Target;
  onSettled: (evaluation?: OpportunitiesResponse) => void;
  onStart?: () => void;
}

/** One action: collect and evaluate. Failure must never be displayed as a fresh evaluation. */
export function MarketDataRefresh({ target, onSettled, onStart }: Props) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const request = useRef<AbortController | null>(null);
  const callback = useRef({ onSettled, onStart });
  callback.current = { onSettled, onStart };
  useEffect(() => () => request.current?.abort(), []);

  const refresh = async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setOutcome(null);
    let notified = false;
    try {
      callback.current.onStart?.();
      const response = await fetch(endpoints[target], {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        if (response.status === 404) throw new Error('本地后端缺少更新接口（HTTP 404），可能是旧后端仍在运行或代理地址错误。请停止旧服务并运行 scripts/start_local.py；本次未完成评估。');
        throw new Error(data?.error?.message || `联网更新失败（HTTP ${response.status}）。`);
      }
      const result = data?.refresh;
      if (result?.target !== target || !['success', 'partial'].includes(result?.status) || typeof result?.message !== 'string') {
        throw new Error('采集返回格式无效，不能认定更新成功。');
      }
      const evaluation = target === 'sectors' ? parseSectorEvaluation(data.evaluation) : undefined;
      if (!controller.signal.aborted) {
        // Use the evaluation returned by this POST; an extra GET could read a different revision.
        notified = true;
        callback.current.onSettled(evaluation);
        setOutcome({ status: result.status, message: result.message });
      }
    } catch (cause: unknown) {
      if (!controller.signal.aborted) setOutcome({ status: 'failed', message: cause instanceof Error ? cause.message : '联网采集失败。' });
    } finally {
      if (!controller.signal.aborted) {
        setBusy(false);
        request.current = null;
        if (!notified) callback.current.onSettled();
      }
    }
  };

  return (
    <div className="market-data-refresh">
      <button className="button secondary" type="button" disabled={busy} onClick={() => void refresh()}>
        <RefreshCw size={16} aria-hidden="true" />
        {busy ? target === 'sectors' ? '正在获取最新数据并评估…' : '正在联网采集…' : target === 'catalog' ? '更新基金目录' : '重新评估'}
      </button>
      {busy && <p role="status">正在访问公开数据源，原有资料暂时保留；采集结束后才计算评估。关闭页面不保证撤销服务器已提交的数据。</p>}
      {outcome && <p role={outcome.status === 'success' ? 'status' : 'alert'}>
        <strong>{outcome.status === 'success' ? target === 'sectors' ? '行情更新及评估完成：' : '更新完成：' : outcome.status === 'partial' ? '部分更新完成：' : '未完成更新：'}</strong>
        {outcome.message}
      </p>}
    </div>
  );
}
