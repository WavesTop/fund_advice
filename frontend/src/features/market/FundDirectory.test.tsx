import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FundDirectory } from './FundDirectory';
import { directoryPage, parseDirectory } from './fund-directory-model';

const result = (code: string, name: string, page = 1) => ({
  items: [{ code, name, share_id: code, fund_type: '测试类型', source_id: 'fixture.catalog' }],
  page, page_size: 20, total: 40, catalog_total: 40, updated_at: '2026-09-15T10:00:00Z',
});
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

describe('real fund directory', () => {
  it('validates identity and does not use partial records', () => {
    expect(parseDirectory(result('000001', '测试基金')).items).toHaveLength(1);
    expect(() => parseDirectory({ items: [{}] })).toThrow();
    for (const value of ['0', '-1', 'nan', '1.5', '999999999999999999999']) expect(directoryPage(value)).toBe(1);
    expect(directoryPage('2')).toBe(2);
  });
  it('restores query and page, and carries them to fund detail', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(result('000001', '测试基金', 2)))));
    render(<MemoryRouter initialEntries={['/funds?q=test&page=2']}><FundDirectory /></MemoryRouter>);
    await screen.findByRole('link', { name: '测试基金' });
    expect(screen.getByLabelText('搜索本地真实基金')).toHaveValue('test');
    const url = new URL(screen.getByRole('link', { name: '测试基金' }).getAttribute('href')!, 'https://example.org');
    expect(url.searchParams.get('from')).toBe('/funds?q=test&page=2');
    expect(screen.getByRole('link', { name: '查看关联研究' }).getAttribute('href')).toContain('fund=000001');
  });
  it('does not let an earlier query replace a later query', async () => {
    let finishOld!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn((path: string) => path.includes('q=old')
      ? new Promise<Response>((resolve) => { finishOld = resolve; })
      : Promise.resolve(new Response(JSON.stringify(result('000002', '新查询基金'))))));
    render(<MemoryRouter initialEntries={['/funds?q=old']}><FundDirectory /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('搜索本地真实基金'), { target: { value: 'new' } });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    await screen.findByRole('link', { name: '新查询基金' });
    finishOld(new Response(JSON.stringify(result('000001', '旧查询基金'))));
    await waitFor(() => expect(screen.queryByRole('link', { name: '旧查询基金' })).not.toBeInTheDocument());
    expect(screen.getByRole('link', { name: '新查询基金' })).toBeInTheDocument();
  });
  it('retains the prior result on same-query refresh failure and permits retry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(async () => new Response(JSON.stringify(result('000001', '测试基金')))).mockRejectedValueOnce(new Error('test offline')));
    render(<MemoryRouter initialEntries={['/funds?q=test']}><FundDirectory /></MemoryRouter>);
    await screen.findByRole('link', { name: '测试基金' });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('link', { name: '测试基金' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
  });
});
