/** Current-data research DTO. No sample funds, inferred mappings, scores or trades. */
export const periodIds = ['short', 'medium', 'long'] as const;
export type PeriodId = (typeof periodIds)[number];
export type EvidenceState = 'watch' | 'conflict' | 'risk' | 'insufficient';
export interface ResearchPeriod {
  id: PeriodId;
  name: string;
  range: string;
  lookback_sessions: number;
  status: 'strong' | 'neutral' | 'weak' | 'insufficient' | 'stale';
  label: string;
  reason: string;
  return_pct: number | null;
  max_drawdown_pct: number | null;
  observation_start?: string;
  observation_end?: string;
  opportunity: {
    status: EvidenceState;
    label: string;
    summary: string;
    supports: string[];
    challenges: string[];
    conditions: string[];
    missing: string[];
  };
  strength?: {
    eligible: boolean;
    rank: number | null;
    percentile: number | null;
    sample_count: number;
    reason: string;
  };
}
export interface FundAssociation {
  code: string;
  name: string;
  status: 'linked' | 'withheld';
  source_id: string | null;
  evidence_url: string | null;
  verified_at: string | null;
  limitations: string[];
}
export interface ResearchItem {
  code: string;
  name: string;
  source_id: string;
  universe_type?: 'hot_board' | 'tracked_index';
  kind?: string;
  as_of: string | null;
  updated_at: string;
  collection_error?: string | null;
  periods: ResearchPeriod[];
  funds: { code: string; name: string }[];
  research?: {
    version: string;
    subject_key: string;
    periods: {
      id: PeriodId;
      evidence_state: EvidenceState;
      market_available: boolean;
      comparison_available: boolean;
      label: string;
      gaps: string[];
      recommendation_status: 'not_evaluated';
      recommendation_reason: string;
    }[];
    fund_associations: FundAssociation[];
    operation_status: 'unavailable';
    operation_reason: string;
  };
}
export interface ResearchResponse {
  method_version: string;
  generated_at: string;
  items: ResearchItem[];
  universe?: {
    label: string;
    scope?: string;
    ranking_as_of: string | null;
    collected_count: number;
    failed_count: number;
    last_error?: string | null;
  };
  advantages?: {
    id: PeriodId;
    name: string;
    horizon: string;
    eligible_count: number;
    message: string;
    items: { code: string; name: string; label: string; reason: string }[];
  }[];
}
export interface FundIdentity {
  code: string;
  name: string;
  share_id: string;
  fund_type: string;
  source_id: string;
}
export interface Selection {
  fund: string;
  sector: string;
  source: string;
  universe: string;
}

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
  if (!['name', 'range', 'label', 'reason'].every((key) => typeof value[key] === 'string')) return false;
  if (!Number.isInteger(value.lookback_sessions) || Number(value.lookback_sessions) <= 0) return false;
  if (!['strong', 'neutral', 'weak', 'insufficient', 'stale'].includes(String(value.status))) return false;
  if (!nullableNumber(value.return_pct) || !nullableNumber(value.max_drawdown_pct)) return false;
  const evidence = value.opportunity;
  if (!record(evidence) || !['watch', 'conflict', 'risk', 'insufficient'].includes(String(evidence.status))) return false;
  if (!['label', 'summary'].every((key) => typeof evidence[key] === 'string')) return false;
  if (!['supports', 'challenges', 'conditions', 'missing'].every((key) => strings(evidence[key]))) return false;
  if (value.strength !== undefined) {
    const strength = value.strength;
    if (!record(strength) || typeof strength.eligible !== 'boolean' || typeof strength.reason !== 'string') return false;
    if (!nullableNumber(strength.rank) || !nullableNumber(strength.percentile)) return false;
    if (!Number.isInteger(strength.sample_count) || Number(strength.sample_count) < 0) return false;
  }
  return true;
}
function validResearch(value: unknown): boolean {
  if (!record(value) || typeof value.version !== 'string' || typeof value.subject_key !== 'string') return false;
  if (value.operation_status !== 'unavailable' || typeof value.operation_reason !== 'string') return false;
  if (!Array.isArray(value.periods) || !Array.isArray(value.fund_associations)) return false;
  const periodValid = value.periods.every((period) => record(period)
    && periodIds.includes(period.id as PeriodId)
    && ['watch', 'conflict', 'risk', 'insufficient'].includes(String(period.evidence_state))
    && typeof period.market_available === 'boolean' && typeof period.comparison_available === 'boolean'
    && typeof period.label === 'string' && strings(period.gaps)
    && period.recommendation_status === 'not_evaluated' && typeof period.recommendation_reason === 'string');
  const fundsValid = value.fund_associations.every((fund) => record(fund)
    && typeof fund.code === 'string' && /^\d{6}$/.test(fund.code) && typeof fund.name === 'string'
    && ['linked', 'withheld'].includes(String(fund.status)) && strings(fund.limitations)
    && ['source_id', 'evidence_url', 'verified_at'].every((key) => fund[key] === null || typeof fund[key] === 'string'));
  return periodValid && fundsValid;
}

export function parseResearchResponse(raw: unknown): ResearchResponse {
  if (!record(raw) || typeof raw.method_version !== 'string' || typeof raw.generated_at !== 'string'
      || !Number.isFinite(Date.parse(raw.generated_at)) || !Array.isArray(raw.items)) {
    throw new Error('研究接口返回格式无效，未使用默认或演示结果。');
  }
  const keys = new Set<string>();
  for (const item of raw.items) {
    if (!record(item) || !['code', 'name', 'source_id', 'updated_at'].every((key) => typeof item[key] === 'string')
        || !(item.as_of === null || typeof item.as_of === 'string')
        || (item.universe_type !== undefined && !['hot_board', 'tracked_index'].includes(String(item.universe_type)))
        || !Array.isArray(item.periods) || !item.periods.every(validPeriod)
        || new Set(item.periods.map((period) => period.id)).size !== item.periods.length
        || !Array.isArray(item.funds) || !item.funds.every((fund) => record(fund) && typeof fund.code === 'string' && typeof fund.name === 'string')
        || (item.research !== undefined && !validResearch(item.research))) {
      throw new Error('研究对象或周期字段无效，未填补缺失数据。');
    }
    const key = subjectKey(item as unknown as ResearchItem);
    if (keys.has(key)) throw new Error('研究对象身份重复，请核查数据来源。');
    keys.add(key);
  }
  if (raw.advantages !== undefined && (!Array.isArray(raw.advantages)
      || !raw.advantages.every((summary) => record(summary) && periodIds.includes(summary.id as PeriodId)
        && typeof summary.horizon === 'string' && typeof summary.message === 'string'
        && Array.isArray(summary.items) && summary.items.every((item) => record(item)
          && ['code', 'name', 'label', 'reason'].every((key) => typeof item[key] === 'string'))))) {
    throw new Error('周期观察列表格式无效。');
  }
  return raw as unknown as ResearchResponse;
}

export function parseFundIdentity(raw: unknown, requestedCode: string): FundIdentity {
  if (!record(raw) || !record(raw.fund) || raw.fund.code !== requestedCode
      || !['code', 'name', 'share_id', 'fund_type', 'source_id'].every((key) => typeof raw.fund === 'object'
        && raw.fund !== null && typeof (raw.fund as Record<string, unknown>)[key] === 'string')) {
    throw new Error('基金身份与请求不一致，已停止展示，未切换到其他基金。');
  }
  return raw.fund as unknown as FundIdentity;
}

export function subjectKey(item: Pick<ResearchItem, 'code' | 'source_id' | 'universe_type'>): string {
  return `${item.universe_type ?? 'tracked_index'}:${item.source_id}:${item.code}`;
}
export function researchHref(item: Pick<ResearchItem, 'code' | 'source_id' | 'universe_type'>, from?: string): string {
  const params = new URLSearchParams({ sector: item.code, source: item.source_id, universe: item.universe_type ?? 'tracked_index' });
  if (from) params.set('from', from);
  return `/advice?${params}`;
}
export function safeReturnPath(value: string | null, fallback: string): string {
  if (!value || !/^\/(funds|sectors|advice)([/?]|$)/.test(value) || value.includes('\\')) return fallback;
  return value;
}
export function selectionFrom(params: URLSearchParams): Selection {
  return { fund: params.get('fund') ?? '', sector: params.get('sector') ?? '', source: params.get('source') ?? '', universe: params.get('universe') ?? '' };
}

export function selectResearchItems(items: ResearchItem[], selection: Selection): { items: ResearchItem[]; problem: string | null } {
  if (selection.fund && !/^\d{6}$/.test(selection.fund)) return { items: [], problem: `基金代码 ${selection.fund} 无效，未替换对象。` };
  if (!selection.sector && (selection.source || selection.universe)) return { items: [], problem: '板块链接缺少代码，请从板块列表重新进入。' };
  let selected = items;
  if (selection.sector) {
    selected = selected.filter((item) => item.code === selection.sector
      && (!selection.source || item.source_id === selection.source)
      && (!selection.universe || (item.universe_type ?? 'tracked_index') === selection.universe));
    if (selected.length > 1) return { items: [], problem: '该代码对应多个来源，请明确板块来源；未按名称猜测。' };
    if (!selected.length) return { items: [], problem: `未找到板块 ${selection.sector} 的当前真实研究资料。` };
  }
  if (selection.fund) {
    selected = selected.filter((item) => item.research?.fund_associations.some((fund) => fund.code === selection.fund && fund.status === 'linked'));
    if (!selected.length) return { items: [], problem: `基金 ${selection.fund} 尚无来源与核验时间完整的关联研究；没有改用其他基金。` };
  }
  return { items: selected, problem: null };
}

/** Uses the server's existing observation list; it does not rank unrelated pools. */
export function periodObservations(data: ResearchResponse, period: PeriodId, selection: Selection): ResearchItem[] {
  const selected = selectResearchItems(data.items, selection);
  if (selected.problem) return [];
  if (selection.fund || selection.sector) return selected.items;
  const summary = data.advantages?.find((entry) => entry.id === period);
  const result: ResearchItem[] = [];
  for (const entry of summary?.items ?? []) {
    const matches = selected.items.filter((item) => item.universe_type === 'hot_board' && item.code === entry.code);
    if (matches.length !== 1) continue;
    const item = matches[0];
    const observation = item.periods.find((entry) => entry.id === period);
    if (!item.collection_error && observation && !['stale', 'insufficient'].includes(observation.status)
        && observation.strength?.eligible && !result.some((old) => subjectKey(old) === subjectKey(item))) result.push(item);
  }
  return result;
}
export function percent(value: number | null): string {
  return value === null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}
export function sourceHref(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

/** Existing decision URLs remain demo-only; live research never records sample trades. */
export function isDemoAdvice(params: URLSearchParams): boolean {
  return params.get('mode') === 'demo' || ['decisions', 'history'].includes(params.get('tab') ?? '');
}
