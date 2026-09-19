type RankingItem = {
  periods: {
    id: string;
    status: string;
    return_pct: number | null;
    strength?: { eligible: boolean };
  }[];
};

/** Explain the safe code-order fallback without promoting stale historical returns. */
export function sectorSortNotice(items: readonly RankingItem[], key: string): string | null {
  if (key === 'heat' || items.length === 0) return null;
  const name = { short: '短期', medium: '中期', long: '长期' }[key];
  if (!name) return null;
  const eligible = items.filter((item) => {
    const period = item.periods.find((value) => value.id === key);
    return period?.strength?.eligible === true &&
      typeof period.return_pct === 'number' && Number.isFinite(period.return_pct) &&
      period.status !== 'stale' && period.status !== 'insufficient';
  }).length;
  if (eligible === 0) {
    return `${name}：当前筛选结果中没有可排名数据，暂按板块代码展示；切换周期不会改变未排名部分的顺序。历史涨跌幅未参与当前排名，请查看缺失日期和采集原因。`;
  }
  return `${name}：当前筛选结果中 ${eligible}/${items.length} 个可比较，按该周期走势排序；其余 ${items.length - eligible} 个放在末尾按代码展示。筛选不改变服务端的同池排名。`;
}
