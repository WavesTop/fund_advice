/** Align observations by source date, never by row position or invented prices. */
export function commonDates(groups: ReadonlyArray<ReadonlyArray<{ date: string }>>): string[] {
  return [...new Set(groups.flatMap((rows) => rows.map((row) => row.date)))].sort();
}

export function alignRows<T extends { date: string }>(
  rows: readonly T[],
  dates: readonly string[],
): Array<T | null> {
  const byDate = new Map(rows.map((row) => [row.date, row]));
  if (byDate.size !== rows.length) throw new Error('序列包含重复日期');
  return dates.map((date) => byDate.get(date) ?? null);
}

export function rangeDates(
  dates: readonly string[],
  range: string,
  start = '',
  end = '',
): string[] {
  if (!dates.length) return [];
  if (range === 'custom') {
    if (!start || !end || start > end || start < dates[0] || end > dates[dates.length - 1])
      return [];
    return dates.filter((date) => date >= start && date <= end);
  }
  const days = ({ month: 31, quarter: 93, half: 186, year: 366 } as Record<string, number>)[range];
  if (!days) return [...dates];
  const cutoff = Date.parse(dates[dates.length - 1] + 'T00:00:00Z') - days * 86400000;
  return dates.filter((date) => Date.parse(date + 'T00:00:00Z') >= cutoff);
}
