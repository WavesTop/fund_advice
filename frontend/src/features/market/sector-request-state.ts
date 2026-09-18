import { parseSectorEvaluation } from './sector-evaluation-model';
import type { OpportunitiesResponse } from './SectorOpportunities';

export type RefreshTarget = 'catalog' | 'sectors';
export type RefreshResult = {
  status: 'success' | 'partial';
  message: string;
  evaluation?: OpportunitiesResponse;
};
const pending = new Map<RefreshTarget, Promise<RefreshResult>>();
const listeners = new Set<() => void>();
let cache: OpportunitiesResponse | null = null;
let revision = 0;

export const readSectorCache = () => cache;
export const sectorCacheRevision = () => revision;
export function writeSectorCache(value: OpportunitiesResponse | null) { cache = value; revision += 1; }
export const pendingRefresh = (target: RefreshTarget) => pending.get(target);
export function subscribeRefresh(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
function notify() { for (const listener of listeners) listener(); }

/** One POST survives route changes; only the explicit button starts collection. */
export function startSharedRefresh(target: RefreshTarget): Promise<RefreshResult> {
  const existing = pending.get(target);
  if (existing) return existing;
  revision += 1; // An earlier GET must not overwrite the result of this refresh.
  const task = (async (): Promise<RefreshResult> => {
    const response = await fetch(target === 'sectors' ? '/api/sectors/refresh' : '/api/funds/catalog/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 404) throw new Error('本地旧后端缺少更新接口（HTTP 404），请核对后端版本并重启服务；本次未完成评估。');
      throw new Error(data?.error?.message || `联网更新失败（HTTP ${response.status}）。`);
    }
    const result = data?.refresh;
    if (result?.target !== target || !['success', 'partial'].includes(result?.status) || typeof result?.message !== 'string') {
      throw new Error('采集返回格式无效，不能认定更新成功。');
    }
    const evaluation = target === 'sectors' ? parseSectorEvaluation(data.evaluation) : undefined;
    if (evaluation) writeSectorCache(evaluation);
    return { status: result.status, message: result.message, evaluation };
  })().finally(() => { pending.delete(target); notify(); });
  pending.set(target, task);
  // Avoid unhandled rejection when every observing component has unmounted.
  void task.catch(() => undefined);
  notify();
  return task;
}
