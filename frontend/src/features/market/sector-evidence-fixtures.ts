/** Synthetic evidence for tests only. No production imports. */
import type { SectorFundamentals } from './sector-evidence-model';
export function evidenceFixture(): SectorFundamentals {
  return {
    status: 'available', scope: '合成成分样本', observed_at: '2026-09-17T02:00:00Z',
    membership_as_of: '2026-09-16', total_members: 2,
    operating: { status: 'available', periods: [{ report_date: '2026-06-30', base_report_date: '2025-06-30',
      metrics: { profit_yoy: { value: '50', covered: 2, total: 2, complete: true,
        current_sum: '30000000', base_sum: '20000000', published_at: '2026-08-01', excluded: [] } } }] },
    valuation: { status: 'available', as_of: '2026-09-16', median_pe_ttm: '25',
      percentile: null, history_count: 1, history_span_days: 0,
      covered: 2, total: 2, positive_count: 2, nonpositive_count: 0, excluded: [] },
    errors: [], limitations: ['合成样本，不是投资建议'], sources: [{ url: 'https://example.test/data', asset_id: 'fixture-asset' }],
    catalysts: { status: 'not_collected', required: ['合成行业需求'] },
  };
}
