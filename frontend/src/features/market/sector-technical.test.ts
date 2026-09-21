import { describe, expect, it } from 'vitest';
import { calculateTechnical, detectTechnicalStages, type TechnicalPoint } from './sector-technical';

const point = (dif: number, dea: number, close = 100): TechnicalPoint => ({
  date: '2026-09-18',
  open: close,
  high: close + 1,
  low: close - 1,
  close,
  dif,
  dea,
  macd: (dif - dea) * 2,
  k: null,
  d: null,
  j: null,
  rsi6: null,
  rsi12: null,
  rsi24: null,
  wr6: null,
  wr10: null,
});

describe('sector technical indicators', () => {
  it('detects MACD underwater golden cross and above-zero death cross', () => {
    expect(detectTechnicalStages([point(-2, -1), point(-0.4, -0.8)])).toContain('underwater-golden-cross');
    expect(detectTechnicalStages([point(2, 1), point(0.5, 0.9)])).toContain('above-zero-death-cross');
  });

  it('detects DIF and DEA near the zero axis relative to latest close', () => {
    expect(detectTechnicalStages([point(0.2, -0.1, 100)])).toEqual([
      'yellow-near-zero',
      'white-near-zero',
    ]);
  });

  it('calculates four indicator families from valid OHLC rows', () => {
    const rows = Array.from({ length: 80 }, (_, index) => {
      const close = 100 + index * 0.25 + Math.sin(index / 3) * 2;
      const day = new Date(Date.UTC(2026, 6, 1 + index)).toISOString().slice(0, 10);
      return {
        date: day,
        open: String(close - 0.3),
        high: String(close + 1),
        low: String(close - 1),
        close: String(close),
      };
    });
    const latest = calculateTechnical(rows).at(-1);
    expect(latest?.dif).toEqual(expect.any(Number));
    expect(latest?.dea).toEqual(expect.any(Number));
    expect(latest?.k).toEqual(expect.any(Number));
    expect(latest?.rsi6).toEqual(expect.any(Number));
    expect(latest?.wr10).toEqual(expect.any(Number));
  });
});
