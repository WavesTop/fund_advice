/** Historical metrics may be displayed without enabling current ranking or advice. */
export type HistoricalPrice = {
  available: true;
  as_of: string;
  label: string;
  return_pct: number;
  ma_bias_pct: number;
  max_drawdown_pct: number;
  note: string;
};

type Period = {
  status: string;
  return_pct: number | null;
  ma_bias_pct?: number | null;
  max_drawdown_pct: number | null;
  historical?: HistoricalPrice;
};

function validDay(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function priceDisplay(period: Period, collectionFailed = false) {
  if (!collectionFailed && ['strong', 'neutral', 'weak'].includes(period.status)) {
    return { available: true, historical: false, asOf: null,
      change: period.return_pct, bias: period.ma_bias_pct ?? null, drawdown: period.max_drawdown_pct };
  }
  const old = period.historical;
  if (old?.available === true && validDay(old.as_of)
      && [old.return_pct, old.ma_bias_pct, old.max_drawdown_pct].every(Number.isFinite)) {
    return { available: true, historical: true, asOf: old.as_of,
      change: old.return_pct, bias: old.ma_bias_pct, drawdown: old.max_drawdown_pct };
  }
  return { available: false, historical: false, asOf: null, change: null, bias: null, drawdown: null };
}
