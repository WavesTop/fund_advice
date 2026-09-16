import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { FundDirectory } from './FundDirectory';
import { MarketDataRefresh } from './MarketDataRefresh';
import { RealSectorDetail } from './RealSectorDetail';
import { SectorHistory } from './SectorHistory';
import { directoryLayout, parseDirectory } from './fund-directory-model';
import { parseSectorDetail, sectorDetailHref } from './sector-detail-model';
import { workbenchCatalog, workbenchDetail, workbenchSector } from './workbench-fixtures';

vi.mock('../../shared/Chart', () => ({ Chart: ({ label, option }: { label: string; option: unknown }) => <div role="img" aria-label={label} data-option={JSON.stringify(option)} /> }));
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function directory(path = '/funds?q=test') {
  return render(<MemoryRouter initialEntries={[path]}><FundDirectory /></MemoryRouter>);
}
function detail() {
  return render(<MemoryRouter initialEntries={[sectorDetailHref(workbenchSector(), '/funds?q=test&page=2', '000001')]}>
    <Routes><Route path="/sectors/:sector" element={<RealSectorDetail />} /></Routes>
  </MemoryRouter>);
}

describe('基金与板块工作台', () => {
  it('根据容器宽度选择6或8张卡片，窄屏保留分页而非长表', () => {
    expect(directoryLayout(1180)).toEqual({ columns: 4, pageSize: 8 });
    expect(directoryLayout(1179)).toEqual({ columns: 3, pageSize: 6 });
    expect(directoryLayout(600)).toEqual({ columns: 2, pageSize: 6 });
    expect(directoryLayout(390)).toEqual({ columns: 1, pageSize: 6 });
  });
  it('卡片展示、下一页及末页，不按基金名称推断关联', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input))));
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
    expect(within(screen.getByRole('list', { name: '基金搜索结果' })).getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByRole('button', { name: '本地目录下一页' })).toBeDisabled();
    expect(String(fetcher.mock.lastCall?.[0])).toContain('page=3&page_size=6');
  });
  it('跨越宽度阈值后切换为8张并回到第一页，不接受旧分页', async () => {
    let resize: (entries: { contentRect: { width: number } }[]) => void = () => {};
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: typeof resize) { resize = callback; }
      observe() {}
      disconnect() {}
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input)))));
    directory('/funds?q=test&page=2');
    await screen.findByRole('link', { name: '测试基金7' });
    act(() => resize([{ contentRect: { width: 1280 } }]));
    await screen.findByRole('link', { name: '测试基金1' });
    expect(screen.getByText(/每页 8 张卡片/)).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: '基金搜索结果' })).getAllByRole('listitem')).toHaveLength(8);
  });
  it('错误分页响应不当作有效搜索结果', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(workbenchCatalog('/api/funds?page_size=8'))));
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
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal('fetch', fetcher);
    const settled = vi.fn();
    render(<MarketDataRefresh target="catalog" onSettled={settled} />);
    fireEvent.click(screen.getByRole('button', { name: '更新基金目录' }));
    expect(screen.getByRole('button')).toBeDisabled();
    fireEvent.click(screen.getByRole('button'));
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith('/api/funds/catalog/refresh', expect.objectContaining({ method: 'POST' }));
    finish(response({ refresh: { target: 'catalog', status: 'success', message: '已导入17条' } }));
    expect(await screen.findByText(/已导入17条/)).toBeInTheDocument();
    expect(settled).toHaveBeenCalledTimes(1);
  });
  it('部分成功不显示全成功；失败后仍核对已提交的数据', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response({ refresh: { target: 'sectors', status: 'partial', message: '部分源失败' }, evaluation: { ...workbenchDetail(), items: [workbenchSector()] } }))
      .mockResolvedValueOnce(response({ error: { message: '采集超时' } }, 504)));
    const settled = vi.fn();
    render(<MarketDataRefresh target="sectors" onSettled={settled} />);
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('部分更新完成');
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('未完成更新：采集超时'));
    expect(settled).toHaveBeenCalledTimes(2);
  });
  it('真实详情按来源与代码读取，并保留返回查询页', async () => {
    const fetcher = vi.fn(async () => response(workbenchDetail()));
    vi.stubGlobal('fetch', fetcher);
    detail();
    await screen.findByRole('heading', { name: '测试关联指数', level: 1 });
    expect(fetcher).toHaveBeenCalledWith('/api/sectors/980017?source_id=fixture.index&universe_type=tracked_index', expect.anything());
    expect(screen.getByRole('link', { name: '返回来源列表' })).toHaveAttribute('href', '/funds?q=test&page=2');
    expect(screen.queryByText('虚构指数样本')).not.toBeInTheDocument();
  });
  it('同代码错源响应不显示为当前板块，也不回退演示', async () => {
    const value = workbenchDetail();
    value.item.source_id = 'wrong';
    vi.stubGlobal('fetch', vi.fn(async () => response(value)));
    detail();
    expect(await screen.findByText(/板块身份与请求不一致/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument();
  });
  it('来源基金关系发生变化后提示暂缓，不把导航参数当作关联证据', async () => {
    const value = workbenchDetail();
    value.item.funds[0].relation_status = 'withheld';
    vi.stubGlobal('fetch', vi.fn(async () => response(value)));
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
    const history = (close: string) => ({ ...item, history_source_id: 'fixture.index', history_source_code: '980017', history_relation: 'native_source', collection_error: null,
      rows: [{ date: '2026-09-15', open: '10', high: '13', low: '9', close }] });
    const fetcher = vi.fn().mockResolvedValueOnce(response(history('11'))).mockResolvedValueOnce(response(history('12')));
    vi.stubGlobal('fetch', fetcher);
    const view = render(<SectorHistory item={item} />);
    fireEvent.click(screen.getByRole('button', { name: '查看真实日线' }));
    await screen.findByRole('img');
    view.rerender(<SectorHistory item={{ ...item, updated_at: '2026-09-16T09:00:00Z' }} />);
    await waitFor(() => expect(screen.getByRole('img')).toHaveAttribute('data-option', expect.stringContaining('[10,12,9,13]')));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
