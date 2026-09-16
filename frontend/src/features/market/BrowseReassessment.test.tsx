import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FundDirectory } from './FundDirectory';
import { MarketDataRefresh } from './MarketDataRefresh';
import { fundVisitKey, readFundVisits, recordFundVisit } from './fund-browse-history';
import { parseSectorEvaluation } from './sector-evaluation-model';
import { workbenchCatalog, workbenchDetail, workbenchSector } from './workbench-fixtures';

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const evaluation = () => ({ ...workbenchDetail(), items: [workbenchSector()] });
function directory(path = '/funds') {
  render(<MemoryRouter initialEntries={[path]}><FundDirectory /></MemoryRouter>);
}

describe('浏览记录和采集后评估', () => {
  it('访问代码校验、去重、最新置顶、容量及损坏缓存恢复', () => {
    localStorage.setItem(fundVisitKey, '{bad json');
    expect(readFundVisits()).toEqual([]);
    for (let i = 1; i <= 70; i += 1) recordFundVisit(String(i).padStart(6, '0'));
    recordFundVisit('000020'); recordFundVisit('不是代码');
    expect(readFundVisits()).toHaveLength(60);
    expect(readFundVisits()[0]).toBe('000020');
    expect(new Set(readFundVisits()).size).toBe(60);
  });
  it('未搜索展示已浏览基金卡片，而不是搜索词对应基金', async () => {
    recordFundVisit('000002'); recordFundVisit('000001');
    localStorage.setItem('fundAdvicer.realFundSearchHistory', JSON.stringify(['没有访问过的关键词']));
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input)))));
    directory();
    const list = await screen.findByRole('list', { name: '基金浏览记录' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(within(list).getAllByRole('article')[0]).toHaveAccessibleName('测试基金1 000001');
    expect(readFundVisits()).toEqual(['000001', '000002']);
  });
  it('清空已提交搜索及未提交输入后回到浏览记录第一页，不删除浏览记录', async () => {
    recordFundVisit('000001');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input)))));
    directory('/funds?q=test&page=2');
    await screen.findByRole('link', { name: '测试基金7' });
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '未提交文本' } });
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    await screen.findByRole('list', { name: '基金浏览记录' });
    expect(screen.getByRole('searchbox')).toHaveValue('');
    expect(screen.getByText(/第 1 \/ 1 页/)).toBeInTheDocument();
    expect(readFundVisits()).toEqual(['000001']);
  });
  it('空浏览记录不兜底展示全库基金', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => response(workbenchCatalog(String(input)))));
    directory();
    expect(await screen.findByText(/暂无浏览记录/)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '基金浏览记录' })).not.toBeInTheDocument();
  });
  it('忽略codes参数的旧后端响应会被拒绝', async () => {
    recordFundVisit('000001');
    vi.stubGlobal('fetch', vi.fn(async () => response(workbenchCatalog('/api/funds?page_size=6'))));
    directory();
    expect(await screen.findByRole('alert')).toHaveTextContent('浏览记录接口不兼容');
    expect(screen.queryByRole('list', { name: '基金浏览记录' })).not.toBeInTheDocument();
  });
  it('清空后迟到的搜索结果不覆盖浏览记录', async () => {
    recordFundVisit('000001');
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).includes('q=old')
      ? new Promise<Response>((resolve) => { finish = resolve; })
      : Promise.resolve(response(workbenchCatalog(String(input))))));
    directory('/funds?q=old');
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    await screen.findByRole('list', { name: '基金浏览记录' });
    await act(async () => finish(response(workbenchCatalog('/api/funds?q=old&page_size=6'))));
    expect(screen.queryByRole('list', { name: '基金搜索结果' })).not.toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: '基金浏览记录' })).getAllByRole('listitem')).toHaveLength(1);
  });
  it('重新评估连续五次POST，每次使用对应的新评估', async () => {
    let round = 0;
    const settled = vi.fn();
    const fetcher = vi.fn(async () => {
      round += 1;
      const value = evaluation(); value.items[0].name = `本次评估${round}`;
      return response({ refresh: { target: 'sectors', status: 'success', message: `第${round}次` }, evaluation: value });
    });
    vi.stubGlobal('fetch', fetcher);
    render(<MarketDataRefresh target="sectors" onSettled={settled} />);
    for (let i = 1; i <= 5; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
      await waitFor(() => expect(settled).toHaveBeenCalledTimes(i));
      expect(settled.mock.lastCall?.[0].items[0].name).toBe(`本次评估${i}`);
    }
    expect(fetcher).toHaveBeenCalledTimes(5);
    expect(fetcher).toHaveBeenLastCalledWith('/api/sectors/refresh', expect.objectContaining({ method: 'POST' }));
    expect(screen.queryByRole('button', { name: '获取最新行业数据' })).not.toBeInTheDocument();
  });
  it('只有采集成功而缺失评估的旧协议不能宣称成功', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ refresh: { target: 'sectors', status: 'success', message: '旧协议' } })));
    render(<MarketDataRefresh target="sectors" onSettled={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('未完成更新');
    expect(screen.queryByText(/行情更新及评估完成/)).not.toBeInTheDocument();
  });
  it('404说明本地接口或代理不匹配，不当成成功或静默重算', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'Not Found' }, 404)));
    render(<MarketDataRefresh target="sectors" onSettled={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('旧后端');
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 404');
  });
  it('缺少三周期或数值无效的结果被拒绝', () => {
    const value = evaluation(); value.items[0].periods.pop();
    expect(() => parseSectorEvaluation(value)).toThrow(/三周期/);
  });
});
