export type PriceRow = {
  date: string;
  open: string;
  high: string;
  low: string;
  close: string;
};

export type TechnicalPoint = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  dif: number | null;
  dea: number | null;
  macd: number | null;
  k: number | null;
  d: number | null;
  j: number | null;
  rsi6: number | null;
  rsi12: number | null;
  rsi24: number | null;
  wr6: number | null;
  wr10: number | null;
};

export type TechnicalStage =
  | 'underwater-golden-cross'
  | 'yellow-near-zero'
  | 'white-near-zero'
  | 'above-zero-death-cross';

export const technicalStageLabels: Record<TechnicalStage, string> = {
  'underwater-golden-cross': '水下金叉',
  'yellow-near-zero': '黄线零轴附近',
  'white-near-zero': '白线零轴附近',
  'above-zero-death-cross': '线上死叉提示',
};

function ema(values: number[], period: number): Array<number | null> {
  const result: Array<number | null> = [];
  const multiplier = 2 / (period + 1);
  let current: number | null = null;
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (index === period - 1) {
      current = values.slice(0, period).reduce((sum, item) => sum + item, 0) / period;
    } else if (index >= period && current != null) {
      current = value * multiplier + current * (1 - multiplier);
    }
    result.push(current);
  }
  return result;
}

function smoothRsi(closes: number[], period: number): Array<number | null> {
  const result: Array<number | null> = Array(closes.length).fill(null);
  if (closes.length <= period) return result;
  let gain = 0;
  let loss = 0;
  for (let index = 1; index <= period; index += 1) {
    const change = closes[index] - closes[index - 1];
    if (change >= 0) gain += change;
    else loss -= change;
  }
  gain /= period;
  loss /= period;
  result[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  for (let index = period + 1; index < closes.length; index += 1) {
    const change = closes[index] - closes[index - 1];
    gain = (gain * (period - 1) + Math.max(change, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-change, 0)) / period;
    result[index] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return result;
}

function wr(points: Array<{ high: number; low: number; close: number }>, period: number): Array<number | null> {
  return points.map((point, index) => {
    if (index < period - 1) return null;
    const sample = points.slice(index - period + 1, index + 1);
    const highest = Math.max(...sample.map((row) => row.high));
    const lowest = Math.min(...sample.map((row) => row.low));
    return highest === lowest ? 50 : ((highest - point.close) / (highest - lowest)) * 100;
  });
}

function validRows(rows: PriceRow[]) {
  return rows
    .map((row) => ({
      date: row.date,
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
    }))
    .filter(
      (row) =>
        /^\d{4}-\d{2}-\d{2}$/.test(row.date) &&
        [row.open, row.high, row.low, row.close].every((value) => Number.isFinite(value) && value > 0) &&
        row.high >= Math.max(row.open, row.close) &&
        row.low <= Math.min(row.open, row.close),
    );
}

export function calculateTechnical(rows: PriceRow[]): TechnicalPoint[] {
  const points = validRows(rows);
  const closes = points.map((row) => row.close);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const dif = closes.map((_, index) =>
    ema12[index] == null || ema26[index] == null ? null : ema12[index]! - ema26[index]!,
  );
  const deaSource = dif.map((value) => value ?? 0);
  const deaRaw = ema(deaSource, 9);
  const rsi6 = smoothRsi(closes, 6);
  const rsi12 = smoothRsi(closes, 12);
  const rsi24 = smoothRsi(closes, 24);
  const wr6 = wr(points, 6);
  const wr10 = wr(points, 10);
  let k = 50;
  let d = 50;
  return points.map((point, index) => {
    let kValue: number | null = null;
    let dValue: number | null = null;
    let jValue: number | null = null;
    if (index >= 8) {
      const sample = points.slice(index - 8, index + 1);
      const highest = Math.max(...sample.map((row) => row.high));
      const lowest = Math.min(...sample.map((row) => row.low));
      const rsv = highest === lowest ? 50 : ((point.close - lowest) / (highest - lowest)) * 100;
      k = (2 * k) / 3 + rsv / 3;
      d = (2 * d) / 3 + k / 3;
      kValue = k;
      dValue = d;
      jValue = 3 * k - 2 * d;
    }
    const dea = index >= 33 ? deaRaw[index] : null;
    const difValue = index >= 25 ? dif[index] : null;
    return {
      ...point,
      dif: difValue,
      dea,
      macd: difValue == null || dea == null ? null : (difValue - dea) * 2,
      k: kValue,
      d: dValue,
      j: jValue,
      rsi6: rsi6[index],
      rsi12: rsi12[index],
      rsi24: rsi24[index],
      wr6: wr6[index],
      wr10: wr10[index],
    };
  });
}

function crossedUp(previous: TechnicalPoint, current: TechnicalPoint): boolean {
  return previous.dif != null && previous.dea != null && current.dif != null && current.dea != null
    && previous.dif <= previous.dea && current.dif > current.dea;
}

function crossedDown(previous: TechnicalPoint, current: TechnicalPoint): boolean {
  return previous.dif != null && previous.dea != null && current.dif != null && current.dea != null
    && previous.dif >= previous.dea && current.dif < current.dea;
}

export function detectTechnicalStages(points: TechnicalPoint[]): TechnicalStage[] {
  const current = points.at(-1);
  const previous = points.at(-2);
  if (!current) return [];
  const base = current.close * 0.003;
  const stages: TechnicalStage[] = [];
  if (
    previous &&
    crossedUp(previous, current) &&
    current.dif != null &&
    current.dea != null &&
    current.dif < 0 &&
    current.dea < 0
  ) {
    stages.push('underwater-golden-cross');
  }
  if (current.dea != null && Math.abs(current.dea) <= base) stages.push('yellow-near-zero');
  if (current.dif != null && Math.abs(current.dif) <= base) stages.push('white-near-zero');
  if (
    previous &&
    crossedDown(previous, current) &&
    current.dif != null &&
    current.dea != null &&
    current.dif > 0 &&
    current.dea > 0
  ) {
    stages.push('above-zero-death-cross');
  }
  return stages;
}
