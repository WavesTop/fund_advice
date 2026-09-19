const retiredSourceMarkers = ['eastmoney', '东方财富', '东财', 'emdata', 'push2', 'datacenterweb'];

export function containsRetiredSourceReference(value: string | null | undefined) {
  if (!value) return false;
  const lowered = value.normalize('NFKC').toLowerCase();
  const compact = lowered.replace(/[^\p{L}\p{N}]/gu, '');
  return retiredSourceMarkers.some((marker) => lowered.includes(marker) || compact.includes(marker));
}

export function sourceLabel(value: string | null | undefined, fallback = '未提供') {
  const normalized = value?.trim();
  if (!normalized) return fallback;
  if (containsRetiredSourceReference(normalized)) {
    return '已退役历史来源（只读、不可用于交易）';
  }
  return normalized;
}
