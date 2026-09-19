import { parseSectorFundamentals } from './sector-evidence-model';

const periodIds = ['short', 'medium', 'long'] as const;
type PeriodId = (typeof periodIds)[number];
type ResearchSubject = {
  code: string;
  source_id: string;
  universe_type?: 'hot_board' | 'tracked_index';
};
type ValidatedResearchResponse = {
  method_version: string;
  generated_at: string;
  items: (ResearchSubject & { periods: unknown[] })[];
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string');
}

function nullableNumber(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value));
}

function validPeriod(value: unknown): boolean {
  if (!record(value) || !periodIds.includes(value.id as PeriodId)) return false;
  if (!['name', 'range', 'label', 'reason'].every((key) => typeof value[key] === 'string'))
    return false;
  if (!Number.isInteger(value.lookback_sessions) || Number(value.lookback_sessions) <= 0)
    return false;
  if (!['strong', 'neutral', 'weak', 'insufficient', 'stale'].includes(String(value.status)))
    return false;
  if (!nullableNumber(value.return_pct) || !nullableNumber(value.max_drawdown_pct)) return false;
  const evidence = value.opportunity;
  if (
    !record(evidence) ||
    !['watch', 'conflict', 'risk', 'insufficient'].includes(String(evidence.status))
  )
    return false;
  if (!['label', 'summary'].every((key) => typeof evidence[key] === 'string')) return false;
  if (!['supports', 'challenges', 'conditions', 'missing'].every((key) => strings(evidence[key])))
    return false;
  if (value.strength !== undefined) {
    const strength = value.strength;
    if (
      !record(strength) ||
      typeof strength.eligible !== 'boolean' ||
      typeof strength.reason !== 'string'
    )
      return false;
    if (!nullableNumber(strength.rank) || !nullableNumber(strength.percentile)) return false;
    if (!Number.isInteger(strength.sample_count) || Number(strength.sample_count) < 0) return false;
  }
  return true;
}

function validResearch(value: unknown): boolean {
  if (!record(value) || typeof value.version !== 'string' || typeof value.subject_key !== 'string')
    return false;
  if (value.operation_status !== 'unavailable' || typeof value.operation_reason !== 'string')
    return false;
  if (!Array.isArray(value.periods) || !Array.isArray(value.fund_associations)) return false;
  const periodValid = value.periods.every(
    (period) =>
      record(period) &&
      periodIds.includes(period.id as PeriodId) &&
      ['watch', 'conflict', 'risk', 'insufficient'].includes(String(period.evidence_state)) &&
      typeof period.market_available === 'boolean' &&
      typeof period.comparison_available === 'boolean' &&
      typeof period.label === 'string' &&
      strings(period.gaps) &&
      period.recommendation_status === 'not_evaluated' &&
      typeof period.recommendation_reason === 'string',
  );
  const fundsValid = value.fund_associations.every(
    (fund) =>
      record(fund) &&
      typeof fund.code === 'string' &&
      /^\d{6}$/.test(fund.code) &&
      typeof fund.name === 'string' &&
      ['linked', 'withheld'].includes(String(fund.status)) &&
      strings(fund.limitations) &&
      ['source_id', 'evidence_url', 'verified_at'].every(
        (key) => fund[key] === null || typeof fund[key] === 'string',
      ),
  );
  return periodValid && fundsValid;
}

/** Validate the real market payload without pulling the advice/demo entry module into this chunk. */
export function parseResearchResponse(raw: unknown): ValidatedResearchResponse {
  if (
    !record(raw) ||
    typeof raw.method_version !== 'string' ||
    typeof raw.generated_at !== 'string' ||
    !Number.isFinite(Date.parse(raw.generated_at)) ||
    !Array.isArray(raw.items)
  ) {
    throw new Error('研究接口返回格式无效，未使用默认结果。');
  }
  const keys = new Set<string>();
  for (const item of raw.items) {
    if (
      !record(item) ||
      !['code', 'name', 'source_id'].every((key) => typeof item[key] === 'string') ||
      !(item.updated_at === null || typeof item.updated_at === 'string') ||
      !(item.as_of === null || typeof item.as_of === 'string') ||
      (item.universe_type !== undefined &&
        !['hot_board', 'tracked_index'].includes(String(item.universe_type))) ||
      !Array.isArray(item.periods) ||
      !item.periods.every(validPeriod) ||
      new Set(item.periods.map((period) => period.id)).size !== item.periods.length ||
      !Array.isArray(item.funds) ||
      !item.funds.every(
        (fund) => record(fund) && typeof fund.code === 'string' && typeof fund.name === 'string',
      ) ||
      (item.research !== undefined && !validResearch(item.research))
    ) {
      throw new Error('研究对象或周期字段无效，未填补缺失数据。');
    }
    if (item.fundamentals !== undefined) parseSectorFundamentals(item.fundamentals);
    const key = subjectKey(item as ResearchSubject);
    if (keys.has(key)) throw new Error('研究对象身份重复，请核查数据来源。');
    keys.add(key);
  }
  if (
    raw.advantages !== undefined &&
    (!Array.isArray(raw.advantages) ||
      !raw.advantages.every(
        (summary) =>
          record(summary) &&
          periodIds.includes(summary.id as PeriodId) &&
          typeof summary.horizon === 'string' &&
          typeof summary.message === 'string' &&
          Array.isArray(summary.items) &&
          summary.items.every(
            (item) =>
              record(item) &&
              ['code', 'name', 'label', 'reason'].every((key) => typeof item[key] === 'string'),
          ),
      ))
  ) {
    throw new Error('周期观察列表格式无效。');
  }
  return raw as unknown as ValidatedResearchResponse;
}

export function subjectKey(item: ResearchSubject): string {
  return `${item.universe_type ?? 'tracked_index'}:${item.source_id}:${item.code}`;
}

export function researchHref(
  item: ResearchSubject,
  from?: string,
): string {
  const params = new URLSearchParams({
    sector: item.code,
    source: item.source_id,
    universe: item.universe_type ?? 'tracked_index',
  });
  if (from) params.set('from', from);
  return `/advice?${params}`;
}

export function safeReturnPath(value: string | null, fallback: string): string {
  if (!value || !/^\/(funds|sectors|advice)([/?]|$)/.test(value) || value.includes('\\'))
    return fallback;
  return value;
}

export function percent(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? '—'
    : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

export function sourceHref(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
