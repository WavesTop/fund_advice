import { afterEach, describe, expect, it, vi } from 'vitest';
import { priceDisplay } from './sector-history-display';
import { researchFixture } from '../advice/research-fixtures';
import { parseSectorEvaluation } from './sector-evaluation-model';
import { readSectorCache, sectorCacheRevision, startSharedRefresh, subscribeRefresh,
  pendingRefresh, writeSectorCache } from './sector-request-state';

function evaluation() {
  const data = researchFixture();
  return parseSectorEvaluation({ ...data, items: data.items.map((item) => ({ ...item,
    periods: item.periods.map((period) => ({ ...period, ma_bias_pct: 1, risk: 'normal' })) })) });
}
const historical = { available: true as const, as_of: '2026-09-16', label: '上涨走势',
  return_pct: 5, ma_bias_pct: 2, max_drawdown_pct: -1, note: '历史，不参与排名' };
const stale = { status: 'stale', return_pct: null, max_drawdown_pct: null, historical };
afterEach(() => { writeSectorCache(null); vi.unstubAllGlobals(); });

describe('历史展示与跨路由采集状态', () => {
  it('历史指标不改变当前状态或比较资格', () => {
    const period = { ...stale, strength: { eligible: false, rank: null } };
    expect(priceDisplay(period)).toMatchObject({ historical: true, change: 5, asOf: '2026-09-16' });
    expect(period.status).toBe('stale');
    expect(period.strength).toEqual({ eligible: false, rank: null });
  });
  it('无效日期与数值不展示，不能只凭available放行', () => {
    for (const change of [{ as_of: '2026-02-30' }, { return_pct: NaN }, { max_drawdown_pct: Infinity }]) {
      expect(priceDisplay({ ...stale, historical: { ...historical, ...change } }).available).toBe(false);
    }
    expect(priceDisplay({ status: 'stale', return_pct: 99, max_drawdown_pct: -1 }).available).toBe(false);
  });
  it('重复点击共用一个请求，所有视图离开后仍保存新结果', async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal('fetch', fetcher);
    const listener = vi.fn(); const unsubscribe = subscribeRefresh(listener);
    const first = startSharedRefresh('sectors');
    const second = startSharedRefresh('sectors');
    expect(second).toBe(first); expect(fetcher).toHaveBeenCalledTimes(1);
    unsubscribe();
    const data = evaluation(); data.items[0].name = '新合成结果';
    finish(new Response(JSON.stringify({ refresh: { target: 'sectors', status: 'success', message: '合成成功' }, evaluation: data })));
    await first;
    expect(readSectorCache()?.items[0].name).toBe('新合成结果');
    expect(pendingRefresh('sectors')).toBeUndefined();
    expect(listener).toHaveBeenCalledTimes(1);
  });
  it('重复读取不触发POST，缓存修订随更新改变', () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    const revision = sectorCacheRevision(); writeSectorCache(evaluation());
    for (let i = 0; i < 5; i++) expect(readSectorCache()).not.toBeNull();
    expect(sectorCacheRevision()).toBeGreaterThan(revision);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('失败保留旧结果并释放请求，下次显式重试可成功', async () => {
    const previous = evaluation(); writeSectorCache(previous);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{}', { status: 502 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ refresh: { target: 'sectors', status: 'partial', message: '合成部分成功' }, evaluation: previous }))));
    await expect(startSharedRefresh('sectors')).rejects.toThrow('HTTP 502');
    expect(readSectorCache()).toBe(previous); expect(pendingRefresh('sectors')).toBeUndefined();
    await expect(startSharedRefresh('sectors')).resolves.toMatchObject({ status: 'partial' });
  });
  it('成功HTTP但缺少评估不能覆盖缓存', async () => {
    const previous = evaluation(); writeSectorCache(previous);
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ refresh: { target: 'sectors', status: 'success', message: '缺字段' } }))));
    await expect(startSharedRefresh('sectors')).rejects.toThrow();
    expect(readSectorCache()).toBe(previous);
  });
});
