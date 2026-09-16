import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import { FundDetailPage } from './index';
import { SectorHistory } from './SectorHistory';
import type { SectorOpportunity } from './SectorOpportunities';
import { failedRefreshFixture, marketDetailFixture } from './market-consistency-fixtures';

vi.mock('../../shared/Chart', () => ({
  Chart: ({
    label,
    option,
    linkGroup,
  }: {
    label: string;
    option: EChartsOption;
    linkGroup?: string;
  }) => (
    <div
      role="img"
      aria-label={label}
      data-option={JSON.stringify(option)}
      data-group={linkGroup}
    />
  ),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function renderFund() {
  return render(
    <MemoryRouter initialEntries={['/funds/510050']}>
      <Routes>
        <Route path="/funds/:code" element={<FundDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}
const board: SectorOpportunity = {
  code: 'BK0001',
  name: '测试板块',
  source_id: 'fixture.board',
  universe_type: 'hot_board',
  updated_at: '2026-09-15T10:00:00Z',
  as_of: '2026-09-15',
  observation_count: 3,
  funds: [],
  periods: [],
};
function boardHistory() {
  return {
    ...board,
    history_source_id: 'fixture.proxy',
    history_source_code: 'proxy1',
    history_relation: 'proxy_not_equivalent',
    collection_error: null,
    rows: marketDetailFixture().related_market.rows,
  };
}

describe('真实图表与关系状态的一致性', () => {
  it('切换已存储净值，按共同日期保留缺值并共享联动分组', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(marketDetailFixture()));
    renderFund();
    await screen.findByRole('img', { name: '510050真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '基金净值' }));
    const chart = await screen.findByRole('img', { name: '510050真实净值走势' });
    const option = JSON.parse(chart.getAttribute('data-option')!);
    expect(option.xAxis.data).toEqual(['2026-09-11', '2026-09-14', '2026-09-15']);
    expect(option.series[0].data).toEqual([1.02, null, 1.04]);
    expect(chart.getAttribute('data-group')).toBe(
      screen.getByRole('img', { name: '000016关联板块真实K线' }).getAttribute('data-group'),
    );
  });
  it('刷新全部失败仍同步核验状态，保留基金行情但不继续绘制失效关联', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(response(marketDetailFixture()))
      .mockResolvedValueOnce(response(failedRefreshFixture(), 502));
    renderFund();
    await screen.findByRole('img', { name: '000016关联板块真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '更新数据' }));
    await screen.findByText('刷新失败，关系核验状态已同步。');
    expect(screen.queryByRole('img', { name: '000016关联板块真实K线' })).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: '510050真实K线' })).toBeInTheDocument();
    expect(screen.getByText(/本轮关系核验失败/)).toBeInTheDocument();
  });
  it('部分成功应用已提交序列并说明失败的分项', async () => {
    const result = {
      ...marketDetailFixture(),
      refresh: {
        status: 'partial',
        stages: {
          fund_series: { status: 'updated', message: '净值已提交' },
          related_market: { status: 'failed', message: '指数暂不可用' },
        },
      },
    };
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(response(marketDetailFixture()))
      .mockResolvedValueOnce(response(result));
    renderFund();
    await screen.findByRole('img', { name: '510050真实K线' });
    fireEvent.click(screen.getByRole('button', { name: '更新数据' }));
    expect(await screen.findByText(/本次部分更新成功/)).toHaveTextContent('指数暂不可用');
  });
  it('明确关系但日线为空时解释缺项', async () => {
    const fixture = marketDetailFixture();
    fixture.related_market.rows = [];
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(fixture));
    renderFund();
    expect(await screen.findByText('关系已记录，但该指数尚无可用日线。')).toBeInTheDocument();
  });
  it('日线按需请求来源精确身份并说明跨源代理边界', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(boardHistory()));
    render(<SectorHistory item={board} />);
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    expect(await screen.findByRole('img', { name: 'BK0001来源真实日线' })).toBeInTheDocument();
    expect(fetcher.mock.calls[0][0]).toBe(
      '/api/sectors/BK0001/series?source_id=fixture.board&universe_type=hot_board',
    );
    expect(screen.getByText(/跨源参考行情/)).toBeInTheDocument();
  });
  it('错源响应不绘图，重试仍请求原对象', async () => {
    const fetcher = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(response({ ...boardHistory(), source_id: 'wrong' }))
      .mockResolvedValueOnce(response(boardHistory()));
    render(<SectorHistory item={board} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await screen.findByText(/行情身份与请求不一致/);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试行情' }));
    await screen.findByRole('img', { name: 'BK0001来源真实日线' });
    expect(fetcher.mock.calls[0][0]).toBe(fetcher.mock.calls[1][0]);
  });
  it('重复行情日期不会静默合并后绘图', async () => {
    const fixture = boardHistory();
    fixture.rows.push({ ...fixture.rows[0] });
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(fixture));
    render(<SectorHistory item={board} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('行情字段无效'));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
