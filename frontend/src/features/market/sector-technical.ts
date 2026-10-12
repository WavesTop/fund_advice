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
  | 'above-zero-golden-cross'
  | 'above-zero-death-cross'
  | 'underwater-death-cross'
  | 'kdj-low-golden-cross'
  | 'kdj-high-death-cross'
  | 'rsi6-low-rebound'
  | 'rsi6-high-fall'
  | 'rsi12-low-rebound'
  | 'rsi12-high-fall'
  | 'rsi24-low-rebound'
  | 'rsi24-high-fall'
  | 'wr6-high-risk'
  | 'wr6-low-repair'
  | 'wr10-high-risk'
  | 'wr10-low-repair';

export const technicalStageLabels: Record<TechnicalStage, string> = {
  'underwater-golden-cross': 'DIF快线上穿DEA慢线，零轴下方金叉',
  'yellow-near-zero': 'DEA慢线接近零轴',
  'white-near-zero': 'DIF快线接近零轴',
  'above-zero-golden-cross': 'DIF快线上穿DEA慢线，零轴上方金叉',
  'above-zero-death-cross': 'DIF快线下穿DEA慢线，零轴上方死叉',
  'underwater-death-cross': 'DIF快线下穿DEA慢线，零轴下方死叉',
  'kdj-low-golden-cross': 'K快速线上穿D慢速线，低位金叉',
  'kdj-high-death-cross': 'K快速线下穿D慢速线，高位死叉',
  'rsi6-low-rebound': 'RSI6短周期低位回升',
  'rsi6-high-fall': 'RSI6短周期高位回落',
  'rsi12-low-rebound': 'RSI12中周期低位回升',
  'rsi12-high-fall': 'RSI12中周期高位回落',
  'rsi24-low-rebound': 'RSI24长周期低位回升',
  'rsi24-high-fall': 'RSI24长周期高位回落',
  'wr6-high-risk': 'WR6短周期接近近期高位风险区',
  'wr6-low-repair': 'WR6短周期接近近期低位修复区',
  'wr10-high-risk': 'WR10中周期接近近期高位风险区',
  'wr10-low-repair': 'WR10中周期接近近期低位修复区',
};

export type TechnicalStageSentiment = 'good' | 'bad' | 'neutral';

export const technicalStageSentiments: Record<TechnicalStage, TechnicalStageSentiment> = {
  'underwater-golden-cross': 'good',
  'yellow-near-zero': 'neutral',
  'white-near-zero': 'good',
  'above-zero-golden-cross': 'good',
  'above-zero-death-cross': 'bad',
  'underwater-death-cross': 'bad',
  'kdj-low-golden-cross': 'good',
  'kdj-high-death-cross': 'bad',
  'rsi6-low-rebound': 'good',
  'rsi6-high-fall': 'bad',
  'rsi12-low-rebound': 'good',
  'rsi12-high-fall': 'bad',
  'rsi24-low-rebound': 'good',
  'rsi24-high-fall': 'bad',
  'wr6-high-risk': 'bad',
  'wr6-low-repair': 'good',
  'wr10-high-risk': 'bad',
  'wr10-low-repair': 'good',
};

export const technicalStageModifiers: Record<TechnicalStage, string> = {
  'underwater-golden-cross': 'DIF/DEA金叉',
  'yellow-near-zero': 'DEA慢线',
  'white-near-zero': 'DIF快线',
  'above-zero-golden-cross': 'DIF/DEA金叉',
  'above-zero-death-cross': 'DIF/DEA死叉',
  'underwater-death-cross': 'DIF/DEA死叉',
  'kdj-low-golden-cross': 'K/D金叉',
  'kdj-high-death-cross': 'K/D死叉',
  'rsi6-low-rebound': 'RSI6',
  'rsi6-high-fall': 'RSI6',
  'rsi12-low-rebound': 'RSI12',
  'rsi12-high-fall': 'RSI12',
  'rsi24-low-rebound': 'RSI24',
  'rsi24-high-fall': 'RSI24',
  'wr6-high-risk': 'WR6',
  'wr6-low-repair': 'WR6',
  'wr10-high-risk': 'WR10',
  'wr10-low-repair': 'WR10',
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

function wr(
  points: Array<{ high: number; low: number; close: number }>,
  period: number,
): Array<number | null> {
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
        [row.open, row.high, row.low, row.close].every(
          (value) => Number.isFinite(value) && value > 0,
        ) &&
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
  return (
    previous.dif != null &&
    previous.dea != null &&
    current.dif != null &&
    current.dea != null &&
    previous.dif <= previous.dea &&
    current.dif > current.dea
  );
}

function crossedDown(previous: TechnicalPoint, current: TechnicalPoint): boolean {
  return (
    previous.dif != null &&
    previous.dea != null &&
    current.dif != null &&
    current.dea != null &&
    previous.dif >= previous.dea &&
    current.dif < current.dea
  );
}

function crossedKdjUp(previous: TechnicalPoint, current: TechnicalPoint): boolean {
  return (
    previous.k != null &&
    previous.d != null &&
    current.k != null &&
    current.d != null &&
    previous.k <= previous.d &&
    current.k > current.d
  );
}

function crossedKdjDown(previous: TechnicalPoint, current: TechnicalPoint): boolean {
  return (
    previous.k != null &&
    previous.d != null &&
    current.k != null &&
    current.d != null &&
    previous.k >= previous.d &&
    current.k < current.d
  );
}

function rsiLowRebound(
  previous: TechnicalPoint | undefined,
  current: TechnicalPoint,
  key: 'rsi6' | 'rsi12' | 'rsi24',
): boolean {
  return (
    !!previous &&
    previous[key] != null &&
    current[key] != null &&
    previous[key] <= 35 &&
    current[key] > previous[key]
  );
}

function rsiHighFall(
  previous: TechnicalPoint | undefined,
  current: TechnicalPoint,
  key: 'rsi6' | 'rsi12' | 'rsi24',
): boolean {
  return (
    !!previous &&
    previous[key] != null &&
    current[key] != null &&
    previous[key] >= 70 &&
    current[key] < previous[key]
  );
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
    crossedUp(previous, current) &&
    current.dif != null &&
    current.dea != null &&
    current.dif > 0 &&
    current.dea > 0
  ) {
    stages.push('above-zero-golden-cross');
  }
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
  if (
    previous &&
    crossedDown(previous, current) &&
    current.dif != null &&
    current.dea != null &&
    current.dif < 0 &&
    current.dea < 0
  ) {
    stages.push('underwater-death-cross');
  }
  if (rsiLowRebound(previous, current, 'rsi6')) {
    stages.push('rsi6-low-rebound');
  }
  if (rsiHighFall(previous, current, 'rsi6')) stages.push('rsi6-high-fall');
  if (rsiLowRebound(previous, current, 'rsi12')) stages.push('rsi12-low-rebound');
  if (rsiHighFall(previous, current, 'rsi12')) stages.push('rsi12-high-fall');
  if (rsiLowRebound(previous, current, 'rsi24')) stages.push('rsi24-low-rebound');
  if (rsiHighFall(previous, current, 'rsi24')) stages.push('rsi24-high-fall');
  if (
    previous &&
    crossedKdjUp(previous, current) &&
    current.k != null &&
    current.d != null &&
    Math.max(current.k, current.d) <= 35
  ) {
    stages.push('kdj-low-golden-cross');
  }
  if (
    previous &&
    crossedKdjDown(previous, current) &&
    current.k != null &&
    current.d != null &&
    Math.min(current.k, current.d) >= 65
  ) {
    stages.push('kdj-high-death-cross');
  }
  if (current.wr6 != null && current.wr6 <= 15) stages.push('wr6-high-risk');
  if (current.wr6 != null && current.wr6 >= 85) stages.push('wr6-low-repair');
  if (current.wr10 != null && current.wr10 <= 20) stages.push('wr10-high-risk');
  if (current.wr10 != null && current.wr10 >= 80) stages.push('wr10-low-repair');
  return stages;
}
