import { describe, expect, it } from 'vitest';
import {
  calculateTechnical,
  detectTechnicalStages,
  technicalStageModifiers,
  type TechnicalPoint,
} from './sector-technical';

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

  it('detects RSI, KDJ and WR stage filters with good or bad modifiers', () => {
    const stages = detectTechnicalStages([
      point(1, 1, 100, { rsi6: 30, k: 20, d: 22, wr10: 50 }),
      point(1, 1, 100, { rsi6: 34, k: 26, d: 24, wr10: 10 }),
    ]);
    expect(stages).toEqual(
      expect.arrayContaining(['rsi-low-rebound', 'kdj-low-golden-cross', 'wr-high-risk']),
    );
    expect(technicalStageModifiers['rsi-low-rebound']).toBe('好');
    expect(technicalStageModifiers['wr-high-risk']).toBe('坏');
    expect(
      detectTechnicalStages([
        point(1, 1, 100, { rsi6: 75, k: 78, d: 74 }),
        point(1, 1, 100, { rsi6: 71, k: 68, d: 70 }),
      ]),
    ).toEqual(expect.arrayContaining(['rsi-high-fall', 'kdj-high-death-cross']));
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
