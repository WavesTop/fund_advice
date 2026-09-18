import { writeSectorCache } from './sector-request-state';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  SectorOpportunities,
  sortSectorItems,
  type SectorOpportunity,
} from './SectorOpportunities';

afterEach(() => {
  writeSectorCache(null);
  cleanup();
  vi.restoreAllMocks();
});

const period = (
  id: 'short' | 'medium' | 'long',
  opportunity: Partial<Record<string, unknown>> = {},
) => ({
  id,
  name: id === 'short' ? '短期' : id === 'medium' ? '中期' : '长期',
  range: '近期',
  lookback_sessions: id === 'short' ? 20 : id === 'medium' ? 60 : 120,
  status: 'strong',
  label: '偏强',
  reason: '价格高于窗口均线',
  return_pct: 3.2,
  ma_bias_pct: 1.4,
  max_drawdown_pct: -2,
  risk: 'normal',
  opportunity: {
    status: 'watch',
    label: '观察',
    summary: '等待更多证据',
    supports: ['走势稳定'],
    challenges: [],
    conditions: [],
    missing: [],
    ...opportunity,
  },
});

function fixture(overrides: Record<string, unknown> = {}) {
  return {
    method_version: 'evidence-screen-v1',
    generated_at: '2026-09-14T00:00:00Z',
    items: [
      {
        code: '000001',
        name: '测试指数',
        source_id: 'local',
        updated_at: '2026-09-14T00:00:00Z',
        as_of: '2026-09-13',
        observation_count: 120,
        funds: [{ code: '510001', name: '测试ETF', relation_status: 'linked' }],
        periods: [period('short'), period('medium'), period('long')],
        ...overrides,
      },
    ],
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/sectors?view=details']}>
      <SectorOpportunities />
    </MemoryRouter>,
  );
}

describe('SectorOpportunities', () => {
  it('changes pages and filters without replacing the original universe ranks', async () => {
    const items = Array.from({ length: 11 }, (_, i) => ({
      ...fixture().items[0],
      code: `BK${1000 + i}`,
      name: `板块${String(i + 1).padStart(2, '0')}`,
      universe_type: 'hot_board',
      heat_rank: i + 1,
      heat_value: (11 - i) * 1e8,
      periods: (['short', 'medium', 'long'] as const).map((id) => ({
        ...period(id),
        return_pct: i,
        strength: {
          eligible: true,
          rank: 11 - i,
          percentile: i * 10,
          sample_count: 11,
          as_of: '2026-09-13',
          reason: '',
        },
      })),
    }));
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response(JSON.stringify({ ...fixture(), items }), { status: 200 })),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '板块01' })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('heading', { name: '板块11' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    expect(screen.getByRole('heading', { name: '板块11' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('搜索板块名称或代码'), { target: { value: '板块03' } });
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '板块03' })).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    expect(screen.getAllByText(/9 \/ 11/)).toHaveLength(3);
    fireEvent.change(screen.getByLabelText('搜索板块名称或代码'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('排序字段'), { target: { value: 'short' } });
    await waitFor(() =>
      expect(screen.getAllByRole('heading', { level: 2 })[0]).toHaveTextContent('板块11'),
    );
    expect(screen.getAllByText(/相对强度分位 100.0\/100/)).toHaveLength(3);
    expect(screen.queryByText(/前 100/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('排序方向'), { target: { value: 'asc' } });
    await waitFor(() =>
      expect(screen.getAllByRole('heading', { level: 2 })[0]).toHaveTextContent('板块01'),
    );
  });

  it('keeps unknown heat last in both directions and preserves strength ties', () => {
    const base = fixture().items[0];
    const items = [
      { ...base, code: 'a', heat_rank: null, periods: [period('short')] },
      {
        ...base,
        code: 'c',
        heat_rank: 1,
        periods: [{ ...period('short'), return_pct: 8, strength: { eligible: true } }],
      },
      {
        ...base,
        code: 'b',
        heat_rank: 2,
        periods: [{ ...period('short'), return_pct: 8, strength: { eligible: true } }],
      },
    ] as SectorOpportunity[];
    expect(sortSectorItems(items, 'heat', 'desc').map((i) => i.code)).toEqual(['c', 'b', 'a']);
    expect(sortSectorItems(items, 'heat', 'asc').map((i) => i.code)).toEqual(['b', 'c', 'a']);
    expect(sortSectorItems(items, 'short', 'desc').map((i) => i.code)).toEqual(['b', 'c', 'a']);
    expect(sortSectorItems(items, 'short', 'asc').map((i) => i.code)).toEqual(['b', 'c', 'a']);
  });

  it('sorts eligible strength first, puts missing last, and uses code as tie break', () => {
    const base = fixture().items[0];
    const items = [
      {
        ...base,
        code: '000002',
        heat_rank: 2,
        periods: [
          period('short'),
          period('medium'),
          {
            ...period('long'),
            return_pct: null,
            strength: {
              eligible: false,
              rank: null,
              percentile: null,
              sample_count: 2,
              as_of: null,
              reason: '缺少',
            },
          },
        ],
      },
      {
        ...base,
        code: '000001',
        heat_rank: 1,
        periods: [
          period('short'),
          period('medium'),
          {
            ...period('long'),
            return_pct: 4,
            strength: {
              eligible: true,
              rank: 1,
              percentile: 10,
              sample_count: 2,
              as_of: '2026-09-13',
              reason: '',
            },
          },
        ],
      },
    ] as SectorOpportunity[];
    expect(sortSectorItems(items, 'long', 'desc').map((item) => item.code)).toEqual([
      '000001',
      '000002',
    ]);
    expect(sortSectorItems(items, 'long', 'asc').map((item) => item.code)).toEqual([
      '000001',
      '000002',
    ]);
    expect(sortSectorItems(items, 'heat', 'desc').map((item) => item.code)).toEqual([
      '000001',
      '000002',
    ]);
  });

  it('renders three period opportunity states and evidence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture()), { status: 200 })),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText('测试指数')).toBeInTheDocument());
    expect(screen.getAllByText(/投资依据：观察/)).toHaveLength(3);
    expect(screen.getByText('测试ETF')).toBeInTheDocument();
  });

  it('shows versioned constituents without inventing portfolio weights', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify(
            fixture({
              membership: {
                status: 'ready',
                as_of: '2026-09-14',
                fetched_at: '2026-09-14T10:00:00Z',
                member_count: 2,
                error: null,
                weight_basis: 'not_provided',
                members: [
                  {
                    stock_code: '000001',
                    stock_name: '示例公司',
                    market: 0,
                    source_order: 1,
                    market_cap: 100,
                    weight: null,
                  },
                ],
              },
            }),
          ),
          { status: 200 },
        ),
      ),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText(/成分股 · 2 只/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/成分股 · 2 只/));
    expect(screen.getByText(/不把总市值当作权重/)).toBeInTheDocument();
    expect(screen.getByText(/示例公司/)).toBeInTheDocument();
  });

  it('keeps a conflict opportunity label when price is strong', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify(
            fixture({
              periods: [
                period('short', {
                  status: 'conflict',
                  label: '冲突',
                  summary: '价格偏强但行业证据相互矛盾',
                  challenges: ['行业数据承压'],
                }),
                period('medium'),
                period('long'),
              ],
            }),
          ),
          { status: 200 },
        ),
      ),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText(/投资依据：冲突/)).toBeInTheDocument());
    expect(screen.getAllByText('偏强')).toHaveLength(3);
    expect(screen.getByText('价格偏强但行业证据相互矛盾')).toBeInTheDocument();
  });

  it('shows empty response and retries after request failure', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...fixture(), items: [] }), { status: 200 }),
      );
    vi.stubGlobal('fetch', fetchMock);
    renderPage();
    await waitFor(() => expect(screen.getByText('真实指数观察暂时无法加载')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getByText('暂时没有真实指数观察')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not replace an unknown valuation date with its observation date', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify(
            fixture({
              valuation: {
                status: 'available',
                as_of: null,
                observed_at: '2026-09-14T12:00:00Z',
                pe_ttm: null,
                source_url: 'https://www.cnindex.com.cn/',
                metrics: [{ label: '动态市盈率', value: 23.8706, unit: '倍' }],
                summary: '缺少同口径历史参照，不能判断便宜或贵。',
              },
            }),
          ),
          { status: 200 },
        ),
      ),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText('估值业务日期未披露')).toBeInTheDocument());
    expect(screen.getByText('资料保存于 2026-09-14')).toBeInTheDocument();
    expect(screen.queryByText(/PE\(TTM\)/)).not.toBeInTheDocument();
  });

  it('does not invent a direction for stale or insufficient periods', async () => {
    const stale = {
      ...period('short', { status: 'watch', label: '研究观察', summary: '行业经营反证仍需核对' }),
      status: 'stale',
      return_pct: 9.9,
    };
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify(fixture({ periods: [stale, period('medium'), period('long')] })),
            { status: 200 },
          ),
        ),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText(/投资依据：研究观察/)).toBeInTheDocument());
    expect(screen.getByText('行情待更新')).toBeInTheDocument();
    expect(screen.getByText('行业经营反证仍需核对')).toBeInTheDocument();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
