import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { FundDirectory } from './FundDirectory';
import { MarketDataRefresh } from './MarketDataRefresh';
import { RealSectorDetail } from './RealSectorDetail';
import { SectorLibrary } from './SectorLibrary';
import { SectorHistory } from './SectorHistory';
import { directoryLayout, parseDirectory } from './fund-directory-model';
import { parseSectorDetail, sectorDetailHref } from './sector-detail-model';
import { writeSectorCache } from './sector-request-state';
import type { OpportunitiesResponse, SectorOpportunity } from './SectorOpportunities';
import { workbenchCatalog, workbenchDetail, workbenchSector } from './workbench-fixtures';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label, option }: { label: string; option: unknown }) => (
    <div role="img" aria-label={label} data-option={JSON.stringify(option)} />
  ),
}));
afterEach(() => {
  cleanup();
  localStorage.clear();
  writeSectorCache(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function board(asOf: string): SectorOpportunity {
  return {
    ...workbenchSector(),
    code: '881001',
    name: '测试行业',
    source_id: 'ths.industry',
    universe_type: 'hot_board',
    heat_rank: 1,
    as_of: asOf,
    updated_at: `${asOf}T08:00:00Z`,
    history_source_id: 'ths.industry',
    history_source_code: '881001',
  };
}
function opportunities(asOf: string): OpportunitiesResponse {
  return {
    method_version: 'fixture-v1',
    generated_at: `${asOf}T08:00:00Z`,
    items: [board(asOf)],
  };
}
function history(asOf: string) {
  const dates: string[] = [];
  const cursor = new Date(`${asOf}T00:00:00Z`);
  while (dates.length < 40) {
    if (cursor.getUTCDay() > 0 && cursor.getUTCDay() < 6) dates.push(cursor.toISOString().slice(0, 10));
    cursor.setUTCDate(cursor.getUTCDate() - 1);
  }
  dates.reverse();
  return {
    code: '881001',
    source_id: 'ths.industry',
    universe_type: 'hot_board',
    as_of: asOf,
    updated_at: `${asOf}T08:00:00Z`,
    history_source_id: 'ths.industry',
    history_source_code: '881001',
    collection_error: null,
    rows: dates.map((date, index) => {
      const close = 10 + index / 10;
      return {
        date,
        open: String(close - 0.1),
        high: String(close + 0.2),
        low: String(close - 0.2),
        close: String(close),
      };
    }),
  };
}
function directory(path = '/funds?q=test') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <FundDirectory />
    </MemoryRouter>,
  );
}
function detail() {
  return render(
    <MemoryRouter
      initialEntries={[sectorDetailHref(workbenchSector(), '/funds?q=test&page=2', '000001')]}
    >
      <Routes>
        <Route path="/sectors/:sector" element={<RealSectorDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('基金与板块工作台', () => {
  it('根据容器宽度选择6或8张卡片，窄屏保留分页而非长表', () => {
    expect(directoryLayout(1180)).toEqual({ columns: 4, pageSize: 8 });
    expect(directoryLayout(1179)).toEqual({ columns: 3, pageSize: 6 });
    expect(directoryLayout(600)).toEqual({ columns: 2, pageSize: 6 });
    expect(directoryLayout(390)).toEqual({ columns: 1, pageSize: 6 });
  });
  it('卡片展示、下一页及末页，不按基金名称推断关联', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) =>
      response(workbenchCatalog(String(input))),
    );
    vi.stubGlobal('fetch', fetcher);
    directory();
    const list = await screen.findByRole('list', { name: '基金搜索结果' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(6);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const link = screen.getByRole('link', { name: '查看测试关联指数详情' });
    const href = new URL(link.getAttribute('href')!, 'https://example.test');
    expect(href.pathname).toBe('/sectors/980017');
    expect(href.searchParams.get('source_id')).toBe('fixture.index');
    expect(href.searchParams.get('universe_type')).toBe('tracked_index');
    fireEvent.click(screen.getByRole('button', { name: '本地目录下一页' }));
    await screen.findByRole('link', { name: '测试基金7' });
    expect(screen.queryByRole('link', { name: '测试基金1' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '本地目录下一页' }));
    await screen.findByRole('link', { name: '测试基金17' });
    expect(
      within(screen.getByRole('list', { name: '基金搜索结果' })).getAllByRole('listitem'),
    ).toHaveLength(5);
    expect(screen.getByRole('button', { name: '本地目录下一页' })).toBeDisabled();
    expect(String(fetcher.mock.lastCall?.[0])).toContain('page=3&page_size=6');
  });
  it('跨越宽度阈值后切换为8张并回到第一页，不接受旧分页', async () => {
    let resize: (entries: { contentRect: { width: number } }[]) => void = () => {};
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: typeof resize) {
          resize = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input)))),
    );
    directory('/funds?q=test&page=2');
    await screen.findByRole('link', { name: '测试基金7' });
    act(() => resize([{ contentRect: { width: 1280 } }]));
    await screen.findByRole('link', { name: '测试基金1' });
    expect(screen.getByText(/每页 8 张卡片/)).toBeInTheDocument();
    expect(
      within(screen.getByRole('list', { name: '基金搜索结果' })).getAllByRole('listitem'),
    ).toHaveLength(8);
  });
  it('错误分页响应不当作有效搜索结果', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response(workbenchCatalog('/api/funds?page_size=8'))),
    );
    directory();
    expect(await screen.findByRole('alert')).toHaveTextContent('分页与本次请求不一致');
    expect(screen.queryByRole('list', { name: '基金搜索结果' })).not.toBeInTheDocument();
  });
  it('无来源的关联字段被拒绝，避免生成错误直达链接', () => {
    const value = workbenchCatalog('/api/funds');
    value.items[0].related_sectors[0].source_id = '';
    expect(() => parseDirectory(value)).toThrow(/核验信息/);
  });
  it('联网更新发送POST并防重复提交，成功后重读本地状态', async () => {
    let finish!: (value: Response) => void;
    const fetcher = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    const settled = vi.fn();
    render(<MarketDataRefresh target="catalog" onSettled={settled} />);
    fireEvent.click(screen.getByRole('button', { name: '更新基金目录' }));
    expect(screen.getByRole('button')).toBeDisabled();
    fireEvent.click(screen.getByRole('button'));
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      '/api/funds/catalog/refresh',
      expect.objectContaining({ method: 'POST' }),
    );
    finish(response({ refresh: { target: 'catalog', status: 'success', message: '已导入17条' } }));
    expect(await screen.findByText(/已导入17条/)).toBeInTheDocument();
    expect(settled).toHaveBeenCalledTimes(1);
  });
  it('部分成功不显示全成功；失败后仍核对已提交的数据', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          response({
            refresh: { target: 'sectors', status: 'partial', message: '部分源失败' },
            evaluation: { ...workbenchDetail(), items: [workbenchSector()] },
          }),
        )
        .mockResolvedValueOnce(response({ error: { message: '采集超时' } }, 504)),
    );
    const settled = vi.fn();
    render(<MarketDataRefresh target="sectors" onSettled={settled} />);
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('部分更新完成');
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('未完成更新：采集超时'),
    );
    expect(settled).toHaveBeenCalledTimes(2);
  });
  it('板块库提供刷新按钮，并用刷新后的行情日期替换旧快照', async () => {
    let finishRefresh!: (value: Response) => void;
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/sectors/opportunities') return response(opportunities('2026-09-23'));
      if (url.startsWith('/api/sectors/881001/series'))
        return response(
          history(
            fetcher.mock.calls.some(([called]) => String(called) === '/api/sectors/refresh')
              ? '2026-10-08'
              : '2026-09-23',
          ),
        );
      if (url === '/api/sectors/refresh') {
        expect(init).toEqual(expect.objectContaining({ method: 'POST' }));
        return new Promise<Response>((resolve) => {
          finishRefresh = resolve;
        });
      }
      if (url === '/api/sectors/collection-status')
        return response({
          state: 'running',
          run_id: 'fixture-refresh',
          result: {
            progress: {
              stage: 'market',
              state: 'running',
              percent: 38,
              message: '同花顺板块采集 38/100',
              current: 38,
              total: 100,
            },
          },
          database_id: 'fixture',
        });
      throw new Error(`Unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
    render(<SectorLibrary />);
    expect((await screen.findAllByText(/行情查询 2026-09-23 16:00:00/)).length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(document.querySelector('.sector-library-summary')).toHaveTextContent(
        '最近交易日 2026-09-23',
      );
      expect(document.querySelector('.sector-library-summary')).toHaveTextContent('+0.72%');
    });
    fireEvent.click(screen.getByRole('button', { name: '刷新板块数据' }));
    expect(await screen.findByRole('status', { name: '板块刷新进度' })).toHaveTextContent('38%');
    finishRefresh(
      response({
        refresh: { target: 'sectors', status: 'success', message: '已刷新到最新交易日' },
        evaluation: opportunities('2026-10-08'),
      }),
    );
    expect((await screen.findAllByText(/行情查询 2026-10-08 16:00:00/)).length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(screen.getByRole('status', { name: '板块刷新进度' })).toHaveTextContent('100%'),
    );
    expect(fetcher).toHaveBeenCalledWith(
      '/api/sectors/refresh',
      expect.objectContaining({ method: 'POST' }),
    );
  });
  it('板块库搜索时展示当前命中数，重复板块不再次添加', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/sectors/opportunities') return response(opportunities('2026-09-23'));
      if (url.startsWith('/api/sectors/881001/series')) return response(history('2026-09-23'));
      if (url === '/api/sector-board-search?q=881001') {
        return response({
          items: [{ code: '881001', name: '测试行业', kind: '行业', source_id: 'ths.industry' }],
        });
      }
      throw new Error(`Unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
    render(<SectorLibrary />);
    await screen.findAllByText(/行情查询 2026-09-23 16:00:00/);
    fireEvent.change(screen.getByLabelText('搜索板块库名称或代码'), {
      target: { value: '881001' },
    });
    expect(screen.getByText(/当前显示 1/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入同花顺板块名称或代码'), {
      target: { value: '881001' },
    });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '测试行业 已在板块库中，已为你定位到列表。',
    );
    expect(fetcher).not.toHaveBeenCalledWith(
      '/api/sectors/custom',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetcher.mock.calls.some(([url]) => String(url).startsWith('/api/sector-board-search'))).toBe(false);
  });
  it('板块库可搜索同花顺板块并添加到同一行情列表', async () => {
    let finishAdd!: (value: Response) => void;
    let addDone = false;
    HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
      this.open = true;
    });
    HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
      this.open = false;
    });
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/sectors/opportunities') return response(opportunities('2026-09-23'));
      if (url.startsWith('/api/sectors/881001/series')) return response(history('2026-09-23'));
      if (url === '/api/sectors/collection-status') {
        if (addDone) {
          return response({
            state: 'success',
            run_id: 'fixture-run',
            result: {
              last_progress: {
                stage: 'market',
                state: 'success',
                percent: 90,
                message: '合成概念 采集完成，正在合并板块库。',
                current: 1,
                total: 1,
              },
            },
            database_id: 'fixture',
          });
        }
        return response({
          state: 'running',
          run_id: 'fixture-run',
          result: {
            progress: {
              stage: 'market',
              state: 'running',
              percent: 42,
              message: '同花顺板块采集 42/100',
              current: 42,
              total: 100,
            },
          },
          database_id: 'fixture',
        });
      }
      if (url === '/api/sector-board-search?q=%E5%90%88%E6%88%90%E6%A6%82%E5%BF%B5') {
        return response({
          items: [
            { code: '886001', name: '合成概念', kind: '概念', source_id: 'sector_daily.ths' },
          ],
        });
      }
      if (url === '/api/sectors/custom') {
        expect(init).toEqual(expect.objectContaining({ method: 'POST' }));
        expect(JSON.parse(String(init?.body)).items[0].code).toBe('886001');
        return new Promise<Response>((resolve) => {
          finishAdd = resolve;
        });
      }
      if (url.startsWith('/api/sectors/886001/series'))
        return response({ ...history('2026-10-08'), code: '886001' });
      throw new Error(`Unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
    render(<SectorLibrary />);
    await screen.findAllByText(/行情查询 2026-09-23 16:00:00/);
    fireEvent.change(screen.getByLabelText('输入同花顺板块名称或代码'), {
      target: { value: '合成概念' },
    });
    fireEvent.click(screen.getByRole('button', { name: '搜索' }));
    const dialog = await screen.findByRole('dialog', { name: '添加板块/指数/ETF' });
    expect(within(dialog).getByText('合成概念')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: '全部添加' }));
    expect(await screen.findByRole('status', { name: '板块刷新进度' })).toHaveTextContent('42%');
    addDone = true;
    finishAdd(
      response({
        added: [{ code: '886001', name: '合成概念', kind: '概念', source_id: 'sector_daily.ths' }],
        evaluation: {
          ...opportunities('2026-10-08'),
          items: [
            board('2026-09-23'),
            {
              ...board('2026-10-08'),
              code: '886001',
              name: '合成概念',
              kind: '概念',
              heat_rank: 2,
            },
          ],
        },
      }),
    );
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(
        '/api/sectors/custom',
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([called]) =>
          String(called).startsWith('/api/sectors/886001/series'),
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(screen.getByRole('status', { name: '板块刷新进度' })).toHaveTextContent('100%'),
    );
    expect(document.querySelector('.sector-library-count')).toHaveTextContent('2 个板块/指数');
    expect(fetcher.mock.calls.filter(([url]) => String(url).startsWith('/api/sectors/881001/series'))).toHaveLength(1);
  });
  it('板块库可编辑删除板块，并同步更新列表数量', async () => {
    HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
      this.open = true;
    });
    HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
      this.open = false;
    });
    const kept = {
      ...board('2026-10-08'),
      code: '881002',
      name: '保留行业',
      heat_rank: 2,
      history_source_code: '881002',
    };
    const initial = {
      ...opportunities('2026-10-08'),
      items: [board('2026-10-08'), kept],
    };
    const afterRemove = {
      ...opportunities('2026-10-08'),
      items: [kept],
    };
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/sectors/opportunities') return response(initial);
      if (url.startsWith('/api/sectors/881001/series')) return response(history('2026-10-08'));
      if (url.startsWith('/api/sectors/881002/series'))
        return response({ ...history('2026-10-08'), code: '881002', history_source_code: '881002' });
      if (url === '/api/sectors/remove') {
        expect(init).toEqual(expect.objectContaining({ method: 'POST' }));
        const body = JSON.parse(String(init?.body));
        expect(body.items).toEqual([
          expect.objectContaining({
            universe_type: 'hot_board',
            source_id: 'ths.industry',
            code: '881001',
            name: '测试行业',
          }),
        ]);
        return response({
          removed: body.items,
          evaluation: afterRemove,
          message: '已从板块库移除 1 个板块/指数。',
        });
      }
      throw new Error(`Unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
    render(<SectorLibrary />);
    await waitFor(() =>
      expect(document.querySelector('.sector-library-count')).toHaveTextContent('2 个板块/指数'),
    );
    fireEvent.click(screen.getByRole('button', { name: '编辑列表' }));
    const dialog = await screen.findByRole('dialog', { name: '编辑板块列表' });
    fireEvent.click(within(dialog).getByLabelText(/测试行业/));
    fireEvent.click(within(dialog).getByRole('button', { name: '删除选中 1 个' }));
    await waitFor(() =>
      expect(document.querySelector('.sector-library-count')).toHaveTextContent('1 个板块/指数'),
    );
    expect(screen.queryByText('测试行业')).not.toBeInTheDocument();
    expect(screen.getByText('保留行业')).toBeInTheDocument();
  });
  it('真实详情按来源与代码读取，并保留返回查询页', async () => {
    const fetcher = vi.fn(async () => response(workbenchDetail()));
    vi.stubGlobal('fetch', fetcher);
    detail();
    await screen.findByRole('heading', { name: '测试关联指数', level: 1 });
    expect(fetcher).toHaveBeenCalledWith(
      '/api/sectors/980017?source_id=fixture.index&universe_type=tracked_index',
      expect.anything(),
    );
    expect(screen.getByRole('link', { name: '返回来源列表' })).toHaveAttribute(
      'href',
      '/funds?q=test&page=2',
    );
    expect(screen.queryByText('虚构指数样本')).not.toBeInTheDocument();
  });
  it('同代码错源响应不显示为当前板块，也不回退演示', async () => {
    const value = workbenchDetail();
    value.item.source_id = 'wrong';
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response(value)),
    );
    detail();
    expect(await screen.findByText(/板块身份与请求不一致/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument();
  });
  it('来源基金关系发生变化后提示暂缓，不把导航参数当作关联证据', async () => {
    const value = workbenchDetail();
    value.item.funds[0].relation_status = 'withheld';
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response(value)),
    );
    detail();
    expect(await screen.findByRole('alert')).toHaveTextContent('当前关系未通过核验');
    expect(screen.getByRole('link', { name: '返回来源基金 000001' })).toBeInTheDocument();
  });
  it('未采集日期保持空值，仍能显示该板块缺项', () => {
    const value = workbenchDetail();
    value.item.updated_at = null;
    expect(parseSectorDetail(value, workbenchSector()).item.updated_at).toBeNull();
  });
  it('更新后重新请求已展开的日线，不继续展示旧图缓存', async () => {
    const item = workbenchSector();
    const history = (close: string) => ({
      ...item,
      history_source_id: 'fixture.index',
      history_source_code: '980017',
      history_relation: 'native_source',
      collection_error: null,
      rows: [{ date: '2026-09-15', open: '10', high: '13', low: '9', close }],
    });
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(response(history('11')))
      .mockResolvedValueOnce(response(history('12')));
    vi.stubGlobal('fetch', fetcher);
    const view = render(<SectorHistory item={item} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await screen.findByRole('img');
    view.rerender(<SectorHistory item={{ ...item, updated_at: '2026-09-16T09:00:00Z' }} />);
    await waitFor(() =>
      expect(screen.getByRole('img')).toHaveAttribute(
        'data-option',
        expect.stringContaining('[10,12,9,13]'),
      ),
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
