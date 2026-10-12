import { describe, expect, it } from 'vitest';
import { calculateTechnical, detectTechnicalStages, type TechnicalPoint } from './sector-technical';

const point = (
  dif: number,
  dea: number,
  close = 100,
  overrides: Partial<TechnicalPoint> = {},
): TechnicalPoint => ({
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
  ...overrides,
});

describe('sector technical indicators', () => {
  it('detects MACD cross stages across strong and weak zones', () => {
    expect(detectTechnicalStages([point(-2, -1), point(-0.4, -0.8)])).toContain(
      'underwater-golden-cross',
    );
    expect(detectTechnicalStages([point(0.5, 0.8), point(1.1, 0.9)])).toContain(
      'above-zero-golden-cross',
    );
    expect(detectTechnicalStages([point(2, 1), point(0.5, 0.9)])).toContain(
      'above-zero-death-cross',
    );
    expect(detectTechnicalStages([point(-1, -2), point(-1.4, -1.2)])).toContain(
      'underwater-death-cross',
    );
  });

  it('detects DIF and DEA near the zero axis relative to latest close', () => {
    expect(detectTechnicalStages([point(0.2, -0.1, 100)])).toEqual([
      'yellow-near-zero',
      'white-near-zero',
    ]);
  });

  it('detects RSI, KDJ and WR stage filters by independent periods', () => {
    const stages = detectTechnicalStages([
      point(1, 1, 100, { rsi6: 30, rsi12: 32, rsi24: 34, k: 20, d: 22, wr6: 50, wr10: 50 }),
      point(1, 1, 100, { rsi6: 34, rsi12: 36, rsi24: 38, k: 26, d: 24, wr6: 10, wr10: 10 }),
    ]);
    expect(stages).toEqual(
      expect.arrayContaining([
        'rsi6-low-rebound',
        'rsi12-low-rebound',
        'rsi24-low-rebound',
        'kdj-low-golden-cross',
        'wr6-high-risk',
        'wr10-high-risk',
      ]),
    );
    expect(
      detectTechnicalStages([
        point(1, 1, 100, { rsi6: 75, rsi12: 74, rsi24: 73, k: 78, d: 74, wr6: 84, wr10: 79 }),
        point(1, 1, 100, { rsi6: 71, rsi12: 69, rsi24: 68, k: 68, d: 70, wr6: 90, wr10: 85 }),
      ]),
    ).toEqual(
      expect.arrayContaining([
        'rsi6-high-fall',
        'rsi12-high-fall',
        'rsi24-high-fall',
        'kdj-high-death-cross',
        'wr6-low-repair',
        'wr10-low-repair',
      ]),
    );
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
