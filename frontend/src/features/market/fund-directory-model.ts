export interface DirectorySector {
  code: string;
  name: string;
  source_id: string;
  universe_type: 'tracked_index';
  relation_source_id: string;
  verified_at: string;
  evidence_url: string;
}
export function directoryLayout(width: number): { columns: number; pageSize: 6 | 8 } {
  return width >= 1180 ? { columns: 4, pageSize: 8 } : { columns: width >= 840 ? 3 : width >= 560 ? 2 : 1, pageSize: 6 };
}
export interface DirectoryFund {
  related_sectors?: DirectorySector[];
  relation_status?: 'linked' | 'withheld' | 'missing';
  relation_reason?: string;
  share_id: string;
  code: string;
  name: string;
  fund_type: string;
  source_id: string;
}
export interface DirectoryResult {
  items: DirectoryFund[];
  page: number;
  page_size: number;
  total: number;
  catalog_total: number;
  updated_at: string | null;
  collection?: { collected: number; failed: number; pending: number } | null;
}
export function directoryPage(value: string | null): number {
  if (!value || !/^[1-9]\d*$/.test(value)) return 1;
  const page = Number(value);
  return Number.isSafeInteger(page) ? page : 1;
}
export function parseDirectory(raw: unknown): DirectoryResult {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw new Error('基金目录返回格式无效。');
  const result = raw as Record<string, unknown>;
  if (!['page', 'page_size', 'total', 'catalog_total'].every((key) => typeof result[key] === 'number'
      && Number.isSafeInteger(result[key]) && Number(result[key]) >= (key === 'page' || key === 'page_size' ? 1 : 0))
      || !(result.updated_at === null || typeof result.updated_at === 'string') || !Array.isArray(result.items)) throw new Error('基金目录分页或日期格式无效。');
  const identities = new Set<string>();
  for (const rawFund of result.items) {
    if (typeof rawFund !== 'object' || rawFund === null || Array.isArray(rawFund)) throw new Error('基金身份格式无效。');
    const fund = rawFund as Record<string, unknown>;
    if (!['share_id', 'code', 'name', 'fund_type', 'source_id'].every((key) => typeof fund[key] === 'string')
        || !/^\d{6}$/.test(String(fund.code)) || !String(fund.share_id) || !String(fund.name)
        || identities.has(String(fund.share_id))) throw new Error('基金身份缺失或重复，未填入示例基金。');
    if (fund.relation_status !== undefined && !['linked', 'withheld', 'missing'].includes(String(fund.relation_status))) throw new Error('基金关联状态无效。');
    if (fund.relation_reason !== undefined && typeof fund.relation_reason !== 'string') throw new Error('基金关联原因格式无效。');
    if (fund.related_sectors !== undefined) {
      if (!Array.isArray(fund.related_sectors) || !fund.related_sectors.every((entry) => {
        if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) return false;
        const sector = entry as Record<string, unknown>;
        return ['code', 'name', 'source_id', 'relation_source_id', 'verified_at', 'evidence_url'].every((key) => typeof sector[key] === 'string' && Boolean(String(sector[key]).trim()))
          && sector.universe_type === 'tracked_index'
          && Number.isFinite(Date.parse(String(sector.verified_at)))
          && /(?:Z|[+-]\d{2}:\d{2})$/.test(String(sector.verified_at))
          && /^https?:\/\//.test(String(sector.evidence_url));
      })) throw new Error('关联板块缺少来源或核验信息，未生成详情链接。');
    }
    if (fund.relation_status === 'linked' && (!Array.isArray(fund.related_sectors) || fund.related_sectors.length !== 1)) throw new Error('已核验关联必须指向唯一板块。');
    if (['withheld', 'missing'].includes(String(fund.relation_status)) && Array.isArray(fund.related_sectors) && fund.related_sectors.length) throw new Error('未核验关联不能提供当前板块链接。');
    identities.add(String(fund.share_id));
  }
  if (result.collection !== undefined && result.collection !== null) {
    const counts = result.collection;
    if (typeof counts !== 'object' || Array.isArray(counts)
        || !['collected', 'failed', 'pending'].every((key) => Number.isSafeInteger((counts as Record<string, unknown>)[key])
          && Number((counts as Record<string, unknown>)[key]) >= 0)) throw new Error('采集状态统计格式无效。');
  }
  return raw as DirectoryResult;
}
