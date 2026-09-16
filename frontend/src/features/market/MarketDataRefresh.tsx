import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';

type Target = 'catalog' | 'sectors';
type Outcome = { status: 'success' | 'partial' | 'failed'; message: string };
const endpoints: Record<Target, string> = {
  catalog: '/api/funds/catalog/refresh',
  sectors: '/api/sectors/refresh',
};

/** A real POST, not a renamed local read. A failed attempt still reloads committed state. */
export function MarketDataRefresh({ target, onSettled }: { target: Target; onSettled: () => void }) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const request = useRef<AbortController | null>(null);
  const callback = useRef(onSettled);
  callback.current = onSettled;
  useEffect(() => () => request.current?.abort(), []);

  const refresh = async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setOutcome(null);
    try {
      const response = await fetch(endpoints[target], {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.error?.message || `联网更新失败（HTTP ${response.status}）。`);
      const result = data?.refresh;
      if (result?.target !== target || !['success', 'partial'].includes(result?.status) || typeof result?.message !== 'string') {
        throw new Error('采集返回格式无效，不能认定更新成功。');
      }
      if (!controller.signal.aborted) setOutcome({ status: result.status, message: result.message });
    } catch (cause: unknown) {
      if (!controller.signal.aborted) setOutcome({ status: 'failed', message: cause instanceof Error ? cause.message : '联网采集失败。' });
    } finally {
      if (!controller.signal.aborted) {
        setBusy(false);
        request.current = null;
        callback.current();
      }
    }
  };

  return (
    <div className="market-data-refresh">
      <button className="button secondary" type="button" disabled={busy} onClick={() => void refresh()}>
        <RefreshCw size={16} aria-hidden="true" />
        {busy ? '正在联网采集…' : target === 'catalog' ? '更新基金目录' : '获取最新行业数据'}
      </button>
      {busy && <p role="status">正在访问公开数据源，原有资料暂时保留。此操作不只是重新计算本地数据。</p>}
      {outcome && <p role={outcome.status === 'success' ? 'status' : 'alert'}>
        <strong>{outcome.status === 'success' ? '更新完成：' : outcome.status === 'partial' ? '部分更新完成：' : '未完成更新：'}</strong>
        {outcome.message}
      </p>}
    </div>
  );
}
