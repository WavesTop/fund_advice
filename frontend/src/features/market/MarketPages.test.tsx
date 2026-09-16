import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom';
import { DemoProvider } from '../../shared/store';
import { FundDetailPage, FundsPage } from './MarketPages';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function browse(path = '/funds') {
  return render(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            element: (
              <DemoProvider>
                <Outlet />
              </DemoProvider>
            ),
            children: [
              { path: '/funds', element: <FundsPage /> },
              { path: '/funds/:code', element: <FundDetailPage /> },
            ],
          },
        ],
        { initialEntries: [path] },
      )}
    />,
  );
}

describe('真实基金目录', () => {
  it('默认只显示总数与搜索案例，不列出基金', async () => {
    const fetchMock = vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [],
          page: 1,
          page_size: 6,
          total: 27843,
          catalog_total: 27843,
          updated_at: '2026-09-13T10:00:00Z',
        }),
        { status: 200 },
      ),
    );
    browse();
    await waitFor(() => expect(screen.getByText(/已收录 27843 条/)).toBeInTheDocument());
    expect(screen.getByText('搜索“510050”')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/funds?q=&page=1&page_size=6', expect.anything());
  });
  it('提交搜索后展示真实结果并链接到详情', async () => {
    const fetchMock = vi
      .mocked(fetch)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [],
            page: 1,
            page_size: 6,
            total: 27843,
            catalog_total: 27843,
            updated_at: null,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                share_id: 'share-1',
                code: '110011',
                name: '真实成长基金',
                fund_type: '混合型',
                source_id: 'catalog',
              },
            ],
            page: 1,
            page_size: 6,
            total: 1,
            catalog_total: 27843,
            updated_at: '2026-09-13T10:00:00Z',
          }),
          { status: 200 },
        ),
      );
    browse();
    await waitFor(() => expect(screen.getByRole('searchbox')).toBeInTheDocument());
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '成长' } });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    await waitFor(() => expect(screen.getByText('真实成长基金')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/funds?q=%E6%88%90%E9%95%BF&page=1&page_size=6',
      expect.anything(),
    );
    expect(screen.getByRole('link', { name: '真实成长基金' })).toHaveAttribute(
      'href',
      expect.stringContaining('/funds/110011'),
    );
  });
  it('保存最近搜索、去重并支持复用和清除', async () => {
    const fetchMock = vi.mocked(fetch).mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            items: [],
            page: 1,
            page_size: 6,
            total: 0,
            catalog_total: 27843,
            updated_at: null,
          }),
          { status: 200 },
        ),
    );
    browse();
    await waitFor(() => expect(screen.getByRole('searchbox')).toBeInTheDocument());
    const input = screen.getByRole('searchbox');
    fireEvent.change(input, { target: { value: '510050' } });
    fireEvent.click(screen.getByRole('button', { name: /^搜索$/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^510050$/ })).toBeInTheDocument(),
    );
    expect(JSON.parse(localStorage.getItem('fundAdvicer.realFundSearchHistory') || '[]')).toEqual([
      '510050',
    ]);
    fireEvent.change(input, { target: { value: '005911' } });
    fireEvent.click(screen.getByRole('button', { name: /^搜索$/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^005911$/ })).toBeInTheDocument(),
    );
    fireEvent.change(input, { target: { value: '510050' } });
    fireEvent.click(screen.getByRole('button', { name: /^搜索$/ }));
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem('fundAdvicer.realFundSearchHistory') || '[]')).toEqual(
        ['510050', '005911'],
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: /^510050$/ }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        '/api/funds?q=510050&page=1&page_size=6',
        expect.anything(),
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: '清除历史' }));
    expect(localStorage.getItem('fundAdvicer.realFundSearchHistory')).toBeNull();
    expect(screen.queryByRole('button', { name: /^510050$/ })).not.toBeInTheDocument();
  });
});

describe('真实基金详情', () => {
  it('展示真实身份并查询真实序列', async () => {
    const fetchMock = vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          fund: {
            share_id: 'share-1',
            code: '110011',
            name: '真实成长基金',
            fund_type: '混合型',
            source_id: 'catalog',
          },
          series: {
            kind: 'nav',
            rows: [{ date: '2026-09-12', unit_nav: '1.23', cumulative_nav: '1.45' }],
            source_id: 'nav-source',
            policy_version: 'v1',
            updated_at: '2026-09-13T10:00:00Z',
          },
        }),
        { status: 200 },
      ),
    );
    browse('/funds/110011');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '真实成长基金' })).toBeInTheDocument(),
    );
    expect(screen.getByText('share-1')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('img', { name: '110011真实净值走势' })).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/funds/110011', expect.anything());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('详情身份 404 显示边界状态', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('', { status: 404 }));
    browse('/funds/999999');
    await waitFor(() => expect(screen.getByText('未找到这只基金')).toBeInTheDocument());
    expect(screen.getByText(/本地真实目录中没有代码 999999/)).toBeInTheDocument();
  });
  it('没有真实序列时明确提示且不展示模拟行情', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          fund: {
            share_id: 'share-2',
            code: '510050',
            name: '上证50ETF',
            fund_type: 'ETF',
            source_id: 'catalog',
          },
          series: null,
        }),
        { status: 200 },
      ),
    );
    browse('/funds/510050');
    await waitFor(() =>
      expect(screen.getByText('该基金尚未采集可展示的净值或 K 线序列。')).toBeInTheDocument(),
    );
    expect(screen.queryByText(/模拟行情|演示行情/)).not.toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
  it('真实价格序列展示 K 线图', async () => {
    const fetchMock = vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          fund: {
            share_id: 'share-3',
            code: '510300',
            name: '沪深300ETF',
            fund_type: 'ETF',
            source_id: 'catalog',
          },
          series: {
            kind: 'price',
            rows: [
              {
                date: '2026-09-12',
                open: '4.10',
                high: '4.20',
                low: '4.00',
                close: '4.15',
                volume: '1000',
                amount: '4150',
              },
            ],
            source_id: 'price-source',
            policy_version: 'v1',
            updated_at: null,
          },
        }),
        { status: 200 },
      ),
    );
    browse('/funds/510300');
    await waitFor(() =>
      expect(screen.getByRole('img', { name: '510300真实K线' })).toBeInTheDocument(),
    );
    expect(screen.getByText('真实 K 线')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('有真实关联板块序列时展示板块 K 线', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          fund: {
            share_id: 'share-4',
            code: '510050',
            name: '上证50ETF',
            fund_type: 'ETF',
            source_id: 'catalog',
          },
          series: null,
          related_market: {
            name: '上证50',
            code: '000016',
            kind: 'index',
            source_id: 'index-source',
            relation_status: 'linked',
            relation_source_id: 'fixture.prospectus',
            verified_at: '2026-09-12T00:00:00Z',
            evidence_url: 'https://example.test/evidence',
            rows: [
              {
                date: '2026-09-12',
                open: '3200',
                high: '3250',
                low: '3180',
                close: '3230',
                volume: '10000',
                amount: '32000000',
              },
            ],
          },
        }),
        { status: 200 },
      ),
    );
    browse('/funds/510050');
    await waitFor(() =>
      expect(screen.getByRole('img', { name: '000016关联板块真实K线' })).toBeInTheDocument(),
    );
    expect(screen.getByText('关联指数行情')).toBeInTheDocument();
  });
  it('没有关联板块时不显示板块行情区块', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          fund: {
            share_id: 'share-5',
            code: '110011',
            name: '真实成长基金',
            fund_type: '混合型',
            source_id: 'catalog',
          },
          series: null,
          related_market: null,
        }),
        { status: 200 },
      ),
    );
    browse('/funds/110011');
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '真实成长基金' })).toBeInTheDocument(),
    );
    expect(screen.queryByText('关联板块行情')).not.toBeInTheDocument();
  });
  it('点击获取后展示刚采集的真实净值', async () => {
    const fund = {
      share_id: '012970',
      code: '012970',
      name: '鹏华国证半导体芯片ETF联接C',
      fund_type: '指数型-股票',
      source_id: 'catalog',
    };
    const fetchMock = vi
      .mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify({ fund, series: null }), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            fund,
            series: {
              kind: 'nav',
              rows: [{ date: '2026-09-11', unit_nav: '1.2345', accumulated_nav: '1.2345' }],
              source_id: 'fund_nav.eastmoney',
              policy_version: 'v1',
              updated_at: '2026-09-14T04:33:53Z',
            },
          }),
          { status: 200 },
        ),
      );
    browse('/funds/012970');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '获取真实数据' })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: '获取真实数据' }));
    await waitFor(() =>
      expect(screen.getByRole('img', { name: '012970真实净值走势' })).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenLastCalledWith('/api/funds/012970/refresh', { method: 'POST' });
  });
  it('数据服务断开时提示重新启动本地网页', async () => {
    const fund = {
      share_id: '012970',
      code: '012970',
      name: '半导体ETF联接C',
      fund_type: '指数型-股票',
      source_id: 'catalog',
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify({ fund, series: null }), { status: 200 }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    browse('/funds/012970');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '获取真实数据' })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: '获取真实数据' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('本地数据服务未连接'));
  });
});
