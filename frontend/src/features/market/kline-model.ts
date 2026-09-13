import { DATA_DATE, funds } from '../../shared/data';

export interface DemoCandle {
  date: string;
  open: number;
  close: number;
  low: number;
  high: number;
  /** Fictional traded fund units, not currency or lots. */
  volume: number;
}

const DAY_MS = 86_400_000;
const price = (value: number) => Number(value.toFixed(4));

function codeSeed(code: string) {
  let seed = 2166136261;
  for (const character of code) seed = Math.imul(seed ^ character.charCodeAt(0), 16777619);
  return seed >>> 0;
}

/** Independent fictional OHLC fixtures. Weekdays are not an exchange trading calendar. */
export function makeDemoCandles(code: string): DemoCandle[] {
  const seed = codeSeed(code);
  const phase = (seed % 628) / 100;
  const base = code === '510300' ? 3.78 : code === '512480' ? 1.12 : 1.5 + (seed % 180) / 100;
  const volatility = code === '512480' ? 1.45 : 1;
  const dailyVolume = code === '512480' ? 9_800_000 : 3_100_000;
  const end = Date.parse(`${DATA_DATE}T00:00:00Z`);
  const candles: DemoCandle[] = [];
  let previousClose = base;

  for (let offset = 365; offset >= 0; offset--) {
    const date = new Date(end - offset * DAY_MS);
    if (date.getUTCDay() === 0 || date.getUTCDay() === 6) continue;

    const index = candles.length;
    const trend = 1 + index * 0.00048;
    const cycle =
      (Math.sin(index / 17 + phase) * 0.055 -
        Math.cos(index / 37 + phase) * 0.037 +
        Math.sin(index * 0.71 + phase) * 0.009) *
      volatility;
    const close = price(base * (trend + cycle));
    const open = price(previousClose * (1 + Math.sin(index * 1.43 + phase) * 0.0024));
    const upperWick = base * (0.002 + (1 + Math.sin(index * 1.13 + phase)) * 0.0025);
    const lowerWick = base * (0.002 + (1 + Math.cos(index * 0.91 + phase)) * 0.0025);
    const activity =
      1 +
      Math.sin(index * 0.53 + phase) * 0.3 +
      Math.cos(index * 0.19 + phase) * 0.2 +
      (Math.abs(close - open) / open) * 18;

    candles.push({
      date: date.toISOString().slice(0, 10),
      open,
      close,
      low: price(Math.min(open, close) - lowerWick),
      high: price(Math.max(open, close) + upperWick),
      volume: Math.round(dailyVolume * activity),
    });
    previousClose = close;
  }
  const quote = funds.find((fund) => fund.code === code && fund.chart === 'price');
  if (!quote) return candles;

  // For price fixtures, the legacy `nav` field holds a fictional traded quote.
  // Anchor the independent OHLC sample to that display quote, never to a NAV series.
  const lastQuote = Number(quote.nav);
  const scale = lastQuote / candles[candles.length - 1].close;
  const anchored = candles.map((candle) => ({
    ...candle,
    open: price(candle.open * scale),
    close: price(candle.close * scale),
    low: price(candle.low * scale),
    high: price(candle.high * scale),
  }));
  const prior = anchored[anchored.length - 2];
  const latest = anchored[anchored.length - 1];
  const priorUpper = prior.high - Math.max(prior.open, prior.close);
  const priorLower = Math.min(prior.open, prior.close) - prior.low;
  prior.close = price(lastQuote / (1 + quote.change / 100));
  prior.low = price(Math.min(prior.open, prior.close) - priorLower);
  prior.high = price(Math.max(prior.open, prior.close) + priorUpper);

  const latestUpper = latest.high - Math.max(latest.open, latest.close);
  const latestLower = Math.min(latest.open, latest.close) - latest.low;
  latest.open = price(prior.close * (1 + Math.sin((anchored.length - 1) * 1.43 + phase) * 0.0024));
  latest.close = lastQuote;
  latest.low = price(Math.min(latest.open, latest.close) - latestLower);
  latest.high = price(Math.max(latest.open, latest.close) + latestUpper);
  return anchored;
}

/** Trailing close-price average; incomplete windows stay missing rather than becoming zero. */
export function movingAverage(candles: DemoCandle[], days: number): Array<number | null> {
  if (!Number.isInteger(days) || days < 1) throw new RangeError('均线窗口必须为正整数。');

  // Work in price ticks so adding/removing a window does not accumulate decimal drift.
  let totalTicks = 0;
  return candles.map((candle, index) => {
    totalTicks += Math.round(candle.close * 10_000);
    if (index >= days) totalTicks -= Math.round(candles[index - days].close * 10_000);
    return index < days - 1 ? null : Math.round(totalTicks / days) / 10_000;
  });
}
