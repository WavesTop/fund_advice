import { parseResearchResponse } from '../advice/research-model';
import type { SectorOpportunity } from './SectorOpportunities';

type Subject = { code: string; source_id: string; universe_type?: 'hot_board' | 'tracked_index' };
export function sectorDetailHref(subject: Subject, from?: string, fromFund?: string): string {
  const params = new URLSearchParams({ source_id: subject.source_id, universe_type: subject.universe_type ?? 'tracked_index' });
  if (from) params.set('from', from);
  if (fromFund) params.set('fromFund', fromFund);
  return `/sectors/${encodeURIComponent(subject.code)}?${params}`;
}
export function parseSectorDetail(raw: unknown, subject: Subject): { item: SectorOpportunity; generated_at: string; method_version: string } {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw new Error('真实板块详情格式无效。');
  const data = raw as Record<string, unknown>;
  const parsed = parseResearchResponse({ ...data, items: [data.item] });
  const item = parsed.items[0] as SectorOpportunity;
  if (item.code !== subject.code || item.source_id !== subject.source_id || item.universe_type !== subject.universe_type) throw new Error('板块身份与请求不一致，未展示其他来源或演示结果。');
  if (item.periods.length !== 3 || !item.periods.every((period) => (period.ma_bias_pct === null || Number.isFinite(period.ma_bias_pct)) && ['normal', 'elevated', 'unknown'].includes(period.risk))) throw new Error('板块三周期指标格式无效。');
  return { item, generated_at: parsed.generated_at, method_version: parsed.method_version };
}
