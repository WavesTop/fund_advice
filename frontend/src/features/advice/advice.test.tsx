import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { initialState } from '../../shared/data';
import { DemoProvider, STORAGE_KEY } from '../../shared/store';
import { AdvicePage } from './AdvicePage';

beforeEach(() => {
  localStorage.clear();
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  });
});
afterEach(cleanup);
const renderPage = (path = '/advice') =>
  render(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: '/advice',
            element: (
              <DemoProvider>
                <AdvicePage />
              </DemoProvider>
            ),
          },
        ],
        { initialEntries: [path] },
      )}
    />,
  );

describe('建议与决定交互', () => {
  it('三个周期同时可见，全部采纳只写入选择记录', async () => {
    const user = userEvent.setup();
    renderPage();
    for (const period of ['短期', '中期', '长期'])
      expect(screen.getByRole('heading', { name: period })).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '记录决定' })[0]);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getAllByRole('radio')).toHaveLength(4);
    await user.click(within(dialog).getByRole('radio', { name: /全部采纳/ }));
    await user.click(within(dialog).getByRole('button', { name: '保存个人决定' }));
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored.decisions).toHaveLength(1);
    expect(stored.decisions[0]).toMatchObject({ choice: '全部采纳', period: 'short', amount: '' });
    expect(stored.transactions).toEqual([]);
    expect(screen.getByRole('dialog', { name: '个人决定 · 当时记录' })).toBeInTheDocument();
  });
  it('部分采纳要求填写金额和理由，保存失败时仍保留完整草稿', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...initialState, scenario: 'error' }));
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getAllByRole('button', { name: '记录决定' })[1]);
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('radio', { name: /部分采纳/ }));
    await user.click(within(dialog).getByRole('button', { name: '保存个人决定' }));
    expect(within(dialog).getByText('部分采纳请填写调整后的计划金额。')).toBeInTheDocument();
    expect(within(dialog).getByText('请说明部分采纳的调整理由。')).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText('调整后的计划金额（元，必填）'), '500.50');
    await user.type(
      within(dialog).getByLabelText('调整理由与具体计划（必填）'),
      '先保留部分现金，继续观察',
    );
    await user.click(within(dialog).getByRole('button', { name: '保存个人决定' }));
    expect(within(dialog).getByLabelText('调整后的计划金额（元，必填）')).toHaveValue('500.50');
    expect(within(dialog).getByLabelText('调整理由与具体计划（必填）')).toHaveValue(
      '先保留部分现金，继续观察',
    );
    expect(screen.getByRole('status')).toHaveTextContent('保存失败');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).decisions).toHaveLength(0);
  });
  it('关联已有交易后交易数量和金额保持不变，重新关联入口排除原记录', async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        ...initialState,
        decisions: [
          {
            id: 'd1',
            fundCode: '510300',
            period: 'short',
            choice: '部分采纳',
            amount: '1000',
            reason: '分批记录',
            createdAt: '2026-09-11T08:00:00Z',
            adviceVersion: '演示建议 v1',
            action: '暂不操作',
          },
        ],
        transactions: [
          {
            id: 't1',
            fundCode: '510300',
            kind: '买入 / 增仓',
            amount: '1000.00',
            shares: '',
            date: '2026-09-11',
            confirmationDate: '',
            settlementDate: '',
            channel: '证券账户',
            status: '待确认',
            fee: '',
            note: '',
            decisionIds: [],
            createdAt: '2026-09-11T10:00:00Z',
          },
        ],
      }),
    );
    const user = userEvent.setup();
    renderPage('/advice?tab=history&decision=d1');
    await user.click(screen.getByRole('button', { name: '关联已有交易' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: '确认关联 1 笔' }));
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored.transactions).toHaveLength(1);
    expect(stored.transactions[0]).toMatchObject({ amount: '1000.00', decisionIds: ['d1'] });
    await user.click(screen.getByRole('button', { name: '关联已有交易' }));
    expect(screen.getByText('暂无可以关联的已有交易')).toBeInTheDocument();
  });
});
