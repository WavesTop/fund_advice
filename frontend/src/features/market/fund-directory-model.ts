export interface DirectoryFund {
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
