import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SectorOpportunities } from './SectorOpportunities';
import { researchFixture } from '../advice/research-fixtures';

function payload() {
  const data = researchFixture();
  return { ...data, items: data.items.map((item) => ({ ...item, observation_count: 121,
    periods: item.periods.map((period) => ({ ...period, ma_bias_pct: 1, risk: 'normal' })) })) };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('sector summary and original evidence', () => {
  it('defaults to a compact three-period summary and retains exact source identity in links', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload()))));
    render(<MemoryRouter initialEntries={['/sectors']}><SectorOpportunities /></MemoryRouter>);
    await screen.findByRole('table', { name: '板块三周期历史表现与研究证据对照' });
    for (const name of ['短期', '中期', '长期']) expect(screen.getByRole('columnheader', { name })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '三周期研究与基金匹配 →' }).getAttribute('href')).toContain('source=fixture.board');
    fireEvent.click(screen.getByRole('button', { name: '完整证据' }));
    expect(screen.getByRole('heading', { name: '测试行业' })).toBeInTheDocument();
    expect(screen.getAllByText('测试反证保留')).toHaveLength(3);
  });
  it('keeps the summary and source dates when refresh fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(async () => new Response(JSON.stringify(payload()))).mockRejectedValueOnce(new Error('test offline')));
    render(<MemoryRouter initialEntries={['/sectors']}><SectorOpportunities /></MemoryRouter>);
    await screen.findByRole('table', { name: '板块三周期历史表现与研究证据对照' });
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('table', { name: '板块三周期历史表现与研究证据对照' })).toBeInTheDocument();
  });
});
