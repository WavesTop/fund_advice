import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StrategyTrialPage } from './StrategyTrialPage';
import { drawdowns, percentage } from './model';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));
afterEach(cleanup);

function show(path = '/trial') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <StrategyTrialPage />
    </MemoryRouter>,
  );
}

describe('read-only research trial', () => {
  it('defaults to the comparator and discloses its frozen scope', () => {
    show();
    expect(screen.getByRole('radio', { name: '分散配置对照' })).toBeChecked();
    expect(screen.getByText('+39.79%')).toBeVisible();
    expect(screen.getByText(/冻结历史截至 2026-10-12/)).toBeVisible();
    expect(screen.getByText(/总资产含未到账款项/)).toBeVisible();
    expect(screen.getByText(/不是独立投资胜率或新入场回测/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /买入|下单/ })).not.toBeInTheDocument();
  });

  it('changes the measured strategy without hiding the comparator', () => {
    show();
    fireEvent.click(screen.getByRole('radio', { name: '趋势加权试验' }));
    expect(screen.getByText('+41.98%')).toBeVisible();
    expect(screen.getAllByText('10.79%').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('radio', { name: '回撤' }));
    expect(screen.getByRole('img', { name: '12主题池两种策略的回撤历史曲线' })).toBeVisible();
  });

  it('shows unavailable old bear periods and actual covered bear returns', () => {
    show();
    fireEvent.click(screen.getByRole('radio', { name: '牛熊区间' }));
    fireEvent.change(screen.getByLabelText('市场阶段'), { target: { value: 'bear' } });
    const region = screen.getByRole('region', { name: '分段收益与回撤表' });
    expect(within(region).getAllByText('历史不足').length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText('历史基金池'), { target: { value: 'long4' } });
    expect(within(region).queryByText('历史不足')).not.toBeInTheDocument();
    expect(within(region).getByText(/2018-01 至 2018-12/)).toBeVisible();
    expect(screen.getByRole('img', { name: /长期宽基池/ })).toBeVisible();
  });

  it('shows losses and labels incomplete years, even with invalid URL settings', () => {
    show('/trial?pool=unknown&policy=unknown');
    const table = screen.getByRole('region', { name: '分段收益与回撤表' });
    expect(within(table).getByText('-1.49%')).toBeVisible();
    expect(within(table).getByText(/2026-01-05 至 2026-10-12 · 部分区间/)).toBeVisible();
    expect(screen.getByRole('radio', { name: '分散配置对照' })).toBeChecked();
  });

  it('reconstructs drawdowns without using future peaks', () => {
    expect(drawdowns([100, 120, 90, 135])).toEqual([0, 0, -25, 0]);
    expect(percentage(-1.49335)).toBe('-1.49%');
  });
});
