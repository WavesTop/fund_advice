/** Synthetic UI contracts, not a market snapshot or an investment result. */
import type { SectorOpportunity } from './SectorOpportunities';

export function workbenchSector(): SectorOpportunity {
  const ids = ['short', 'medium', 'long'] as const;
  const names = ['短期', '中期', '长期'] as const;
  return {
    code: '980017', name: '测试关联指数', source_id: 'fixture.index', universe_type: 'tracked_index',
    updated_at: '2026-09-16T08:00:00Z', as_of: '2026-09-15', observation_count: 121,
    funds: [{ code: '000001', name: '测试基金1', relation_status: 'linked' }],
    periods: ids.map((id, index) => ({
      id, name: names[index], range: '测试观察窗口', lookback_sessions: [20, 60, 120][index],
      status: 'strong', label: '上涨走势', reason: '测试行情描述', return_pct: 2,
      ma_bias_pct: 1, max_drawdown_pct: -2, risk: 'normal',
      opportunity: { status: 'insufficient', label: '依据不足', summary: '测试缺项', supports: [], challenges: [], conditions: [], missing: ['缺少经营证据'] },
    })),
  };
}
export function workbenchCatalog(url: string) {
  const params = new URL(url, 'https://example.test').searchParams;
  const page = Number(params.get('page') || 1);
  const size = Number(params.get('page_size') || 6);
  const items = Array.from({ length: 17 }, (_, index) => ({
    code: String(index + 1).padStart(6, '0'), name: `测试基金${index + 1}`,
    share_id: `fixture-${index + 1}`, fund_type: 'ETF', source_id: 'fixture.catalog',
    relation_status: index === 0 ? 'linked' : 'missing',
    relation_reason: index === 0 ? '明确跟踪关系。' : '尚无已核验板块关联。',
    related_sectors: index === 0 ? [{
      code: '980017', name: '测试关联指数', source_id: 'fixture.index', universe_type: 'tracked_index',
      relation_source_id: 'fixture.prospectus', verified_at: '2026-09-15T08:00:00Z', evidence_url: 'https://example.test/prospectus',
    }] : [],
  }));
  const selected = params.has('codes') ? [...new Set((params.get('codes') ?? '').split(','))]
    .map((code) => items.find((item) => item.code === code)).filter((item): item is typeof items[number] => item !== undefined) : items;
  return { selection_mode: params.has('codes') ? 'codes' : 'query', items: selected.slice((page - 1) * size, page * size), page, page_size: size, total: selected.length, catalog_total: 17, updated_at: '2026-09-16T08:00:00Z' };
}
export function workbenchDetail() {
  return { item: workbenchSector(), generated_at: '2026-09-16T08:00:00Z', method_version: 'fixture-v1' };
}
