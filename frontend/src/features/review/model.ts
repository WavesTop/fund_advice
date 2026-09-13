import type { DemoState } from '../../shared/types';

export type StrategyVersion = 'v1.0' | 'v1.1';

/** Adoption is an append-only demo event; old advice retains its original version. */
export function adoptStrategy(
  state: DemoState,
  version: StrategyVersion,
  at: string,
  id: string,
): DemoState {
  if (state.strategyVersion === version) return state;
  return {
    ...state,
    strategyVersion: version,
    strategyHistory: [
      ...state.strategyHistory,
      {
        id,
        version,
        at,
        reason:
          version === 'v1.0'
            ? '本人确认恢复 v1.0，仅改变演示中的当前版本，历史记录保持原样。'
            : '本人确认启用候选 v1.1，仅用于界面演示；有效性尚未验证。',
      },
    ],
  };
}

export const reviewSamples = {
  dates: ['08-12', '08-17', '08-22', '08-27', '09-01', '09-06', '09-11'],
  capital: [10000, 10000, 10000, 10000, 11000, 11000, 11000],
  profit: [
    [0, 95, 45, 178, 178, 270, 330],
    [0, 76, 55, 130, 130, 220, 265],
    [0, 88, 125, 218, 218, 310, 425],
    [0, 60, 112, 180, 180, 278, 360],
  ],
  // Fixture rates are supplied display values, not a ledger calculation.
  rate: [
    [0, 0.95, 0.45, 1.78, 1.78, 2.44, 2.99],
    [0, 0.76, 0.55, 1.3, 1.3, 2.0, 2.4],
    [0, 0.88, 1.25, 2.18, 2.18, 2.78, 3.82],
    [0, 0.6, 1.12, 1.8, 1.8, 2.52, 3.27],
  ],
};
