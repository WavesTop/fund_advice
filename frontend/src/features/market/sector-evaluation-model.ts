import { parseResearchResponse } from '../advice/research-model';
import type { OpportunitiesResponse } from './SectorOpportunities';

/** Reuse research identity/evidence checks; a successful import alone is not an evaluation. */
export function parseSectorEvaluation(raw: unknown): OpportunitiesResponse {
  const result = parseResearchResponse(raw);
  for (const item of result.items) {
    if (item.periods.length !== 3 || !item.periods.every((period) => {
      const value = period as unknown as { ma_bias_pct: unknown; risk: unknown };
      return (value.ma_bias_pct === null || Number.isFinite(value.ma_bias_pct))
        && ['normal', 'elevated', 'unknown'].includes(String(value.risk));
    })) throw new Error('板块评估三周期指标无效，不能认定评估完成。');
  }
  return result as OpportunitiesResponse;
}
