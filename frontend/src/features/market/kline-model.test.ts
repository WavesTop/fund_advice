import { describe, expect, it } from 'vitest';
import { DATA_DATE, funds } from '../../shared/data';
import { makeDemoCandles, movingAverage, type DemoCandle } from './kline-model';

const candle = (close: number, index: number): DemoCandle => ({
  date: `2026-09-${String(index + 1).padStart(2, '0')}`,
  open: close,
  close,
  low: close,
  high: close,
  volume: 1_000,
});

describe('deterministic OHLC display fixtures', () => {
  it('returns stable fresh samples and different shapes for the two ETFs', () => {
    const first = makeDemoCandles('510300');
    const second = makeDemoCandles('510300');
    const semiconductor = makeDemoCandles('512480');
    expect(first).toEqual(second);
    expect(first).not.toBe(second);
    expect(first).not.toEqual(semiconductor);
    expect(first.map((item) => item.volume)).not.toEqual(semiconductor.map((item) => item.volume));
    first[0].close = 99;
    expect(makeDemoCandles('510300')).toEqual(second);
  });

  it('covers roughly one year of unique ascending weekdays ending at the fixture date', () => {
    const samples = makeDemoCandles('510300');
    expect(samples.length).toBeGreaterThanOrEqual(250);
    expect(samples.length).toBeLessThanOrEqual(263);
    expect(samples.at(-1)?.date).toBe(DATA_DATE);
    expect(new Set(samples.map((item) => item.date)).size).toBe(samples.length);
    for (let index = 0; index < samples.length; index++) {
      const date = new Date(`${samples[index].date}T00:00:00Z`);
      expect([0, 6]).not.toContain(date.getUTCDay());
      if (index > 0) expect(samples[index].date > samples[index - 1].date).toBe(true);
    }
  });

  it.each(['510300', '512480'])(
    'anchors the final traded price and daily change for %s',
    (code) => {
      const samples = makeDemoCandles(code);
      const quote = funds.find((fund) => fund.code === code)!;
      const lastClose = samples.at(-1)!.close;
      const priorClose = samples.at(-2)!.close;
      expect(lastClose).toBe(Number(quote.nav));
      expect(Number(((lastClose / priorClose - 1) * 100).toFixed(2))).toBe(quote.change);
    },
  );

  it.each(['510300', '512480'])(
    'keeps valid OHLC bounds, price precision and volume for %s',
    (code) => {
      const samples = makeDemoCandles(code);
      expect(samples.some((item) => item.close > item.open)).toBe(true);
      expect(samples.some((item) => item.close < item.open)).toBe(true);
      for (const item of samples) {
        expect(item.low).toBeGreaterThan(0);
        expect(item.low).toBeLessThanOrEqual(Math.min(item.open, item.close));
        expect(item.high).toBeGreaterThanOrEqual(Math.max(item.open, item.close));
        expect(item.high).toBeGreaterThan(item.low);
        for (const value of [item.open, item.close, item.low, item.high]) {
          expect(Number.isFinite(value)).toBe(true);
          expect(value).toBe(Number(value.toFixed(4)));
        }
        expect(Number.isInteger(item.volume)).toBe(true);
        expect(item.volume).toBeGreaterThan(0);
      }
    },
  );
});

describe('trailing moving average', () => {
  it('keeps warm-up periods missing and calculates only the trailing close window', () => {
    const samples = [1, 2, 6, 10, 5].map(candle);
    expect(movingAverage(samples, 3)).toEqual([null, null, 3, 6, 7]);
    expect(movingAverage(samples, 1)).toEqual([1, 2, 6, 10, 5]);
    expect(movingAverage(samples, 9)).toEqual([null, null, null, null, null]);
    expect(movingAverage([], 5)).toEqual([]);
  });

  it('does not change earlier averages when future prices are appended or modified', () => {
    const samples = makeDemoCandles('510300');
    const prefix = samples.slice(0, 60);
    const averages = movingAverage(samples, 20);
    expect(movingAverage(prefix, 20)).toEqual(averages.slice(0, 60));
    const changedFuture = samples.map((item, index) =>
      index < 60 ? item : { ...item, close: 100 },
    );
    expect(movingAverage(changedFuture, 20).slice(0, 60)).toEqual(averages.slice(0, 60));
  });

  it('rounds average output to four decimals and does not mutate source candles', () => {
    const samples = [1.1111, 1.1112, 1.1113, 1.2345].map(candle);
    const before = structuredClone(samples);
    expect(movingAverage(samples, 3)).toEqual([null, null, 1.1112, 1.1523]);
    expect(samples).toEqual(before);
  });

  it.each([0, -1, 1.5, NaN, Infinity])('rejects an invalid window %s', (days) => {
    expect(() => movingAverage([], days)).toThrow(RangeError);
  });
});
