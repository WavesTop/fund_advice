import { describe, expect, it } from 'vitest';
import { initialState } from '../../shared/data';
import { adoptStrategy } from './model';

describe('demo strategy adoption', () => {
  it('appends enable and restore events without rewriting an old decision', () => {
    const original = structuredClone(initialState);
    original.decisions.push({
      id: 'old',
      fundCode: '510300',
      period: 'short',
      choice: '全部采纳',
      amount: '100',
      reason: '当时理由',
      createdAt: '2026-08-01T10:00:00Z',
      adviceVersion: 'v1.0',
      action: '观察',
    });
    const enabled = adoptStrategy(original, 'v1.1', '2026-09-01T10:00:00Z', 'enable');
    const restored = adoptStrategy(enabled, 'v1.0', '2026-09-02T10:00:00Z', 'restore');
    expect(restored.strategyVersion).toBe('v1.0');
    expect(restored.strategyHistory.map((event) => event.version)).toEqual(['v1.1', 'v1.0']);
    expect(restored.strategyHistory[0]).toEqual(enabled.strategyHistory[0]);
    expect(restored.decisions).toEqual(original.decisions);
    expect(restored.decisions[0].adviceVersion).toBe('v1.0');
    expect(original.strategyHistory).toEqual([]);
  });

  it('does not create another adoption event for the already active version', () => {
    expect(adoptStrategy(initialState, 'v1.0', '2026-09-01T10:00:00Z', 'duplicate')).toBe(
      initialState,
    );
  });
});
