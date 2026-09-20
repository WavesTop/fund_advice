const retiredSourceMarkers = ['eastmoney', '东方财富', '东财', 'emdata', 'push2', 'datacenterweb'];
const sourceLabels: Record<string, string> = {
  'fund_nav.ths': '同花顺基金净值（候选事实）',
  'index_daily.sina': '新浪指数行情（候选事实）',
  'index_daily.csi_official': '中证官方指数行情',
  'fund_profile.ths+cni_index.official': '同花顺基金资料 + 国证官方目录（当前关联）',
  'fund_profile.ths+csi_index.official': '同花顺基金资料 + 中证官方目录（当前关联）',
};

export function containsRetiredSourceReference(value: string | null | undefined) {
  if (!value) return false;
  const lowered = value.normalize('NFKC').toLowerCase();
  const compact = lowered.replace(/[^\p{L}\p{N}]/gu, '');
  return retiredSourceMarkers.some(
    (marker) => lowered.includes(marker) || compact.includes(marker),
  );
}

export function sourceLabel(value: string | null | undefined, fallback = '未提供') {
  const normalized = value?.trim();
  if (!normalized) return fallback;
  if (containsRetiredSourceReference(normalized)) {
    return '已退役历史来源（只读、不可用于交易）';
  }
  return sourceLabels[normalized] ?? normalized;
}
