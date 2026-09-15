import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ResearchAdvicePage } from './ResearchAdvicePage';
import { linkedFixture, researchFixture } from './research-fixtures';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const renderPage = (path = '/advice') => render(<MemoryRouter initialEntries={[path]}><ResearchAdvicePage /></MemoryRouter>);

describe('real research page', () => {
  it('renders all three periods and does not invent an operation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(researchFixture()))));
    renderPage();
    await screen.findByRole('heading', { name: '短期' });
    for (const name of ['中期', '长期']) expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '记录决定' })).not.toBeInTheDocument();
    expect(screen.getByText(/基金推荐 \/ 个人操作/)).toBeInTheDocument();
  });
  it('retains a full prior result when a refresh fails', async () => {
    const fetcher = vi.fn().mockImplementationOnce(async () => new Response(JSON.stringify(researchFixture())))
      .mockRejectedValueOnce(new Error('test offline'));
    vi.stubGlobal('fetch', fetcher);
    renderPage();
    await screen.findByRole('heading', { name: '短期' });
    fireEvent.click(screen.getByRole('button', { name: '重新评估本地资料' }));
    await screen.findByRole('alert');
    expect(screen.getByText(/本次更新失败，保留上次结果/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '长期' })).toBeInTheDocument();
  });
  it('keeps an unknown fund unknown instead of showing a sample', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => path.includes('/api/funds/')
      ? new Response('{}', { status: 404 }) : new Response(JSON.stringify(researchFixture()))));
    renderPage('/advice?fund=999999');
    await screen.findByRole('alert');
    expect(screen.getByText(/本地真实目录没有基金 999999/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '短期' })).not.toBeInTheDocument();
  });
  it('displays provenance for the exact linked fund only', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(path.includes('/api/funds/')
      ? { fund: { code: '000001', name: '测试关联基金', share_id: 'test-share', fund_type: '测试类型', source_id: 'fixture.catalog' } }
      : linkedFixture()))));
    renderPage('/advice?fund=000001');
    await screen.findByRole('heading', { name: '短期' });
    expect(screen.getByRole('link', { name: '关系来源' })).toHaveAttribute('href', 'https://example.org/test-prospectus');
    expect(screen.getByText('仅合成关联，不是推荐')).toBeInTheDocument();
  });
  it('shows a precise object-not-found state for an invalid sector link', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(researchFixture()))));
    renderPage('/advice?sector=missing&source=fixture.board');
    await waitFor(() => expect(screen.getByText(/未找到板块 missing/)).toBeInTheDocument());
    expect(screen.queryByRole('heading', { name: '短期' })).not.toBeInTheDocument();
  });
  it('ignores a late fund response after the selected code changes', async () => {
    let finishOld!: (value: Response) => void;
    const identity = (code: string, name: string) => ({ fund: {
      code, name, share_id: `test-${code}`, fund_type: '测试类型', source_id: 'fixture.catalog',
    } });
    vi.stubGlobal('fetch', vi.fn((path: string) => {
      if (path.endsWith('/000001')) return new Promise<Response>((resolve) => { finishOld = resolve; });
      if (path.endsWith('/000002')) return Promise.resolve(new Response(JSON.stringify(identity('000002', '新查询基金'))));
      return Promise.resolve(new Response(JSON.stringify(linkedFixture())));
    }));
    renderPage('/advice?fund=000001');
    fireEvent.change(screen.getByLabelText('研究基金代码'), { target: { value: '000002' } });
    fireEvent.click(screen.getByRole('button', { name: '查看基金研究' }));
    await screen.findByRole('heading', { name: '新查询基金 · 000002' });
    await act(async () => { finishOld(new Response(JSON.stringify(identity('000001', '旧查询基金')))); });
    expect(screen.getByRole('heading', { name: '新查询基金 · 000002' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '旧查询基金 · 000001' })).not.toBeInTheDocument();
  });

});
