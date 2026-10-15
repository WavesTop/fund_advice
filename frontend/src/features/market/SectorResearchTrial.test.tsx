import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SectorOpportunities } from './SectorOpportunities';
import snapshot from './sector-research-snapshot.json';
import { candidateRows, researchView } from './sector-research-model';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
async function show(query = '') {
  render(
    <MemoryRouter initialEntries={[`/sectors?view=research${query}`]}>
      <SectorOpportunities />
    </MemoryRouter>,
  );
  await screen.findByRole('heading', { name: '板块机会 · 趋势研究' });
}
describe('sector opportunity three-horizon research', () => {
  it('is reachable inside sectors, frozen and research-only, without market requests', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    await show();
    expect(screen.getByRole('heading', { name: '板块机会 · 趋势研究' })).toBeVisible();
    expect(screen.getByRole('link', { name: '趋势研究试用' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(screen.getByText(/候选与价格依据：2026-10-13/)).toBeVisible();
    expect(screen.getByText(/历史输入截止：2026-10-14/)).toBeVisible();
    expect(screen.getByLabelText('研究规则')).toHaveValue('compare');
    expect(screen.getByText(/当前行业名单存在存续偏差/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /买入|下单/ })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('preserves original rule ranks under search and changes evidence with the horizon', async () => {
    await show('&rule=maximin_top10');
    const period = snapshot.periods[0];
    const code = period.selections.maximin_top10[3];
    fireEvent.change(screen.getByLabelText('板块搜索'), { target: { value: code } });
    const table = screen.getByRole('region', { name: '三周期研究候选表' });
    expect(within(table).getByText('收益与回撤序位折中 #4')).toBeVisible();
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    fireEvent.change(screen.getByLabelText('板块搜索'), { target: { value: '' } });
    const history = screen.getByRole('region', { name: '连续账户历史检验' });
    expect(within(history).getByText('+115.04%')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: /中期/ }));
    expect(within(history).getByText('+73.00%')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: /长期/ }));
    expect(within(history).getByText('+64.70%')).toBeVisible();
    expect(screen.getByRole('img', { name: /长期冻结价格路径/ })).toBeVisible();
  });
  it('does not fill an empty uptrend selection or retain an unrelated chart', async () => {
    await show('&horizon=long&rule=upward_quality');
    expect(screen.getByText(/当日没有板块满足该规则。数据齐全/)).toBeVisible();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: '新入场收益检验' })).getByText('-12.49%'),
    ).toBeVisible();
  });
  it('exposes recent losses, old bear gaps and all stress outcomes', async () => {
    await show('&rule=maximin_top10');
    expect(
      within(screen.getByRole('region', { name: '新入场收益检验' })).getByText('-10.17%'),
    ).toBeVisible();
    fireEvent.click(screen.getByText('牛熊阶段与覆盖缺口'));
    expect(
      within(screen.getByRole('region', { name: '牛熊阶段检验' })).getAllByText('历史不足').length,
    ).toBeGreaterThan(0);
    fireEvent.click(screen.getByText('费用、复核时点与执行敏感性 · 全部 9 组'));
    const stress = screen.getByRole('region', { name: '敏感性检验' });
    expect(within(stress).getByText('-35.67%')).toBeVisible();
    expect(within(stress).getAllByRole('row')).toHaveLength(10);
  });
  it('uses safe defaults for invalid parameters and a deterministic comparison union', async () => {
    await show('&horizon=bogus&rule=bogus');
    expect(screen.getByRole('tab', { name: /短期/ })).toHaveAttribute('aria-selected', 'true');
    expect(researchView('all_industries')).toBe('compare');
    const period = snapshot.periods[0];
    const codes = candidateRows(period, 'compare', '').map((row) => row.code);
    expect(codes).toEqual([...new Set(Object.values(period.selections).flat())].sort());
  });
  it('supports keyboard horizon navigation', async () => {
    await show();
    fireEvent.keyDown(screen.getByRole('tab', { name: /短期/ }), { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: /中期/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /中期/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', { name: /中期/ }), { key: 'End' });
    expect(screen.getByRole('tab', { name: /长期/ })).toHaveAttribute('aria-selected', 'true');
  });
  it('separates price uptrends from the empty model veto across all three horizons', async () => {
    await show('&rule=price_state');
    const diagnostic = screen.getByRole('note', { name: '价格方向与模型筛选诊断' });
    expect(diagnostic).toHaveTextContent('短期价格稳定上行 1 个');
    expect(diagnostic).toHaveTextContent('两者交集 0 个');
    const table = screen.getByRole('region', { name: '三周期研究候选表' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByRole('button', { name: /小家电/ })).toBeVisible();
    expect(screen.getByText(/不能当作这份价格观察名单的收益/)).toBeVisible();
    const accountReference = screen.getByRole('region', { name: '连续账户历史检验' });
    expect(accountReference.closest('details')).not.toHaveAttribute('open');
    expect(accountReference).not.toBeVisible();
    fireEvent.click(screen.getByText('旧模型连续账户参考 · 与价格观察名单不同'));
    expect(accountReference.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('region', { name: '连续账户历史检验' })).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: /中期/ }));
    expect(within(table).getAllByRole('row')).toHaveLength(10);
    expect(diagnostic).toHaveTextContent('中期价格稳定上行 9 个');
    expect(
      within(screen.getByRole('region', { name: '价格上行状态历史检验' })).getByText('-0.49%'),
    ).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: /长期/ }));
    expect(screen.getByText(/当日没有板块处于稳定上行或趋势增强状态/)).toBeVisible();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
  it('does not silently convert missing price-state evidence into an empty list', () => {
    expect(() => candidateRows(snapshot.periods[0], 'price_state', '')).toThrow(/unavailable/);
    expect(researchView('price_state')).toBe('price_state');
  });
});
