import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom';
import { DemoProvider, STORAGE_KEY } from '../../shared/store';
import { FundDetailPage, FundsPage, SectorDetailPage } from './MarketPages';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute('open');
  };
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
              { path: '/sectors/:sector', element: <SectorDetailPage /> },
            ],
          },
        ],
        { initialEntries: [path] },
      )}
    />,
  );
}

describe('基金页面交互与资料边界', () => {
  it('搜索独立显示同名份额并在进入详情后保留返回条件', () => {
    browse('/funds?q=均衡&type=混合型');
    expect(screen.getByRole('searchbox', { name: '搜索基金名称或代码' })).toHaveValue('均衡');
    expect(screen.getByText('000001')).toBeInTheDocument();
    expect(screen.getByText('000002')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: '查看 000002 详情' }));
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('C类');
    fireEvent.click(screen.getByRole('link', { name: '返回基金库' }));
    expect(screen.getByRole('searchbox', { name: '搜索基金名称或代码' })).toHaveValue('均衡');
    expect(screen.getByRole('combobox', { name: '基金类型' })).toHaveValue('混合型');
  });

  it('未知代码不伪造身份或创建补录成功任务，格式错误保留输入', () => {
    browse();
    fireEvent.click(screen.getByRole('button', { name: '补充基金' }));
    const dialog = within(screen.getByRole('dialog'));
    fireEvent.change(dialog.getByLabelText('基金代码'), { target: { value: 'abc' } });
    fireEvent.click(dialog.getByRole('button', { name: '查询演示身份' }));
    expect(dialog.getByRole('alert')).toHaveTextContent('6 位数字');
    expect(dialog.getByLabelText('基金代码')).toHaveValue('abc');
    fireEvent.change(dialog.getByLabelText('基金代码'), { target: { value: '999998' } });
    fireEvent.click(dialog.getByRole('button', { name: '查询演示身份' }));
    expect(dialog.getByRole('alert')).toHaveTextContent('无法确认名称和份额类别');
    expect(dialog.queryByRole('link', { name: /确认身份/ })).not.toBeInTheDocument();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('已收录代码先核对份额身份，重复查询不新增任务', () => {
    browse();
    fireEvent.click(screen.getByRole('button', { name: '补充基金' }));
    const dialog = within(screen.getByRole('dialog'));
    fireEvent.change(dialog.getByLabelText('基金代码'), { target: { value: '000002' } });
    fireEvent.click(dialog.getByRole('button', { name: '查询演示身份' }));
    fireEvent.click(dialog.getByRole('button', { name: '查询演示身份' }));
    expect(dialog.getByText('已收录，无需重复补录')).toBeInTheDocument();
    expect(dialog.getByRole('heading', { level: 3 })).toHaveTextContent('C类');
    expect(dialog.getByRole('link', { name: /确认身份/ })).toHaveAttribute('href', '/funds/000002');
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('仅净值基金无交易K线切换，时间范围不隐藏任何分析周期', () => {
    browse('/funds/000001');
    expect(screen.getByText('仅有净值，无交易 K 线')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '交易价格' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '近 1 月' }));
    for (const name of ['短期', '中期', '长期'])
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '自定义' }));
    fireEvent.change(screen.getByLabelText('起始日期'), { target: { value: '2026-09-12' } });
    expect(screen.getByRole('alert')).toHaveTextContent('有效起止日期');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('历史基金不提供新交易入口或事后伪造的历史分析', () => {
    browse('/funds/009999');
    expect(screen.getByText('历史基金 · 已停止运作')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '录入持仓' })).not.toBeInTheDocument();
    expect(screen.getByText('历史分析快照暂缺')).toBeInTheDocument();
    expect(screen.getByRole('img')).toBeInTheDocument();
  });

  it('ETF 穿透与未知部分不被合并，行业选择联动股票而主题说明重叠', () => {
    browse('/funds/001100');
    expect(screen.getByText('已穿透 22.77% · 未穿透 77.23%')).toBeInTheDocument();
    expect(screen.getByText('74.7%')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '先进制造' }));
    expect(screen.getByRole('row', { name: /示例精密科技/ })).toHaveClass('market-highlight');
    fireEvent.click(screen.getByRole('button', { name: '主题' }));
    expect(screen.getByText('主题可能重叠，各项比例不可直接求和')).toBeInTheDocument();
    expect(screen.getByText('国产替代')).toBeInTheDocument();
  });

  it('板块缺失趋势时不绘制伪曲线，并保留来源基金入口', () => {
    browse('/sectors/清洁能源?fromFund=001102');
    expect(screen.getByText('板块趋势资料尚未收录')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '返回来源基金' })).toHaveAttribute(
      'href',
      '/funds/001102',
    );
    expect(screen.getByRole('heading', { name: '相关基金' })).toBeInTheDocument();
  });
});
