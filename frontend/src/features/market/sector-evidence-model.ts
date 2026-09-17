/** Source-exact collected evidence. Missing data never becomes a synthetic zero. */
export interface EvidenceMetric {
  value: string | null;
  covered: number;
  total: number;
  complete: boolean;
  published_at?: string | null;
  current_sum?: string | null;
  base_sum?: string | null;
  excluded: { code: string; reason: string }[];
}
export interface SectorFundamentals {
  status: string;
  scope: string;
  observed_at: string | null;
  membership_as_of: string | null;
  total_members: number;
  observation_id?: number | null;
  operating: { status: string; periods: {
    report_date: string;
    base_report_date: string | null;
    metrics: Record<string, EvidenceMetric>;
  }[] };
  valuation: {
    status: string;
    as_of?: string | null;
    index_pe_ttm?: string | null;
    median_pe_ttm?: string | null;
    percentile: string | null;
    total?: number;
    covered?: number;
    positive_count?: number;
    nonpositive_count?: number;
    history_count: number;
    history_span_days: number;
    excluded?: { code: string; reason: string }[];
  };
  errors: string[];
  limitations: string[];
  sources: { url: string; asset_id: string }[];
  catalysts: { status: string; required: string[] };
}
const labels: Record<string, string> = {
  not_collected: '尚未采集', collecting: '采集中／尚未完成', available: '资料可用',
  partial: '部分样本可用', missing: '缺少有效样本', failed: '采集失败',
  blocked: '核验未通过', stale: '资料过期', membership_mismatch: '成分已变化或核验失败',
};
export function evidenceStatus(status: string): string { return labels[status] ?? '状态待核验'; }
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((v) => typeof v === 'string');
const count = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0;
const status = (value: unknown) => typeof value === 'string' && Object.hasOwn(labels, value);
const decimal = (value: unknown) => value === null || typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value));
const date = (value: unknown) => value === null || typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const timestamp = (value: unknown) => value === null || typeof value === 'string' && Number.isFinite(Date.parse(value));
const excluded = (value: unknown) => Array.isArray(value) && value.every((row) => record(row) && typeof row.code === 'string' && typeof row.reason === 'string');
function metric(value: unknown): boolean {
  return record(value) && decimal(value.value) && count(value.covered) && count(value.total)
    && Number(value.covered) <= Number(value.total) && typeof value.complete === 'boolean'
    && (!value.complete || value.covered === value.total) && excluded(value.excluded)
    && (value.published_at === undefined || timestamp(value.published_at))
    && (value.current_sum === undefined || decimal(value.current_sum))
    && (value.base_sum === undefined || decimal(value.base_sum));
}
export function parseSectorFundamentals(raw: unknown): SectorFundamentals {
  const invalid = () => { throw new Error('板块经营/估值证据格式无效，未填补缺项。'); };
  if (!record(raw) || !status(raw.status) || typeof raw.scope !== 'string' || !timestamp(raw.observed_at)
    || !date(raw.membership_as_of) || !count(raw.total_members) || !strings(raw.errors) || !strings(raw.limitations)
    || !Array.isArray(raw.sources) || !raw.sources.every((v) => record(v) && typeof v.url === 'string' && typeof v.asset_id === 'string')
    || !record(raw.catalysts) || !status(raw.catalysts.status) || !strings(raw.catalysts.required)
    || !record(raw.operating) || !status(raw.operating.status) || !Array.isArray(raw.operating.periods)
    || !raw.operating.periods.every((p) => record(p) && p.report_date !== null && date(p.report_date)
      && date(p.base_report_date) && record(p.metrics) && Object.values(p.metrics).every(metric))) return invalid();
  const v = raw.valuation;
  if (!record(v) || !status(v.status) || !decimal(v.percentile) || !count(v.history_count) || !count(v.history_span_days)
    || !['index_pe_ttm', 'median_pe_ttm'].every((key) => v[key] === undefined || decimal(v[key]))
    || !['total', 'covered', 'positive_count', 'nonpositive_count'].every((key) => v[key] === undefined || count(v[key]))
    || (v.as_of !== undefined && !date(v.as_of)) || (v.excluded !== undefined && !excluded(v.excluded))
    || (v.total !== undefined && v.covered !== undefined && Number(v.covered) > Number(v.total))
    || (v.percentile !== null && (Number(v.percentile) < 0 || Number(v.percentile) > 100))) return invalid();
  return raw as unknown as SectorFundamentals;
}
