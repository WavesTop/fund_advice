import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { DemoProvider, STORAGE_KEY } from '../../shared/store';
import { initialState } from '../../shared/data';
import { PortfolioPage } from './PortfolioPage';

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
const renderPage = (path = '/portfolio') =>
  render(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: '/portfolio',
            element: (
              <DemoProvider>
                <PortfolioPage />
              </DemoProvider>
            ),
          },
        ],
        { initialEntries: [path] },
      )}
    />,
  );

describe('持仓表单交互', () => {
  it('现金未录入时展示未知，切换条件后可明确保存为零', async () => {
    const user = userEvent.setup();
    renderPage();
    expect(screen.getByText('未知不等于零，总资产暂不可用')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^投资条件$/ }));
    await user.type(screen.getByLabelText('已知可用现金（元）'), '0');
    await user.click(screen.getByRole('button', { name: '保存投资条件' }));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).conditions.cash).toBe('0');
  });
  it('三步录入保留金额草稿，保存待确认记录而不修改现金', async () => {
    const user = userEvent.setup();
    renderPage('/portfolio?fund=510300');
    await user.click(screen.getByRole('button', { name: '＋ 记录实际交易' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('交易渠道'), '证券账户');
    await user.click(within(dialog).getByRole('button', { name: '下一步' }));
    await user.type(within(dialog).getByLabelText('申请 / 发生日期'), '2026-09-11');
    await user.type(within(dialog).getByLabelText('金额 / 已记录成本（元）'), '1200.50');
    await user.click(within(dialog).getByRole('button', { name: '上一步' }));
    await user.click(within(dialog).getByRole('button', { name: '下一步' }));
    expect(within(dialog).getByLabelText('金额 / 已记录成本（元）')).toHaveValue('1200.50');
    await user.click(within(dialog).getByRole('button', { name: '下一步' }));
    await user.click(within(dialog).getByRole('button', { name: '保存演示记录' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(saved.transactions).toHaveLength(1);
    expect(saved.transactions[0]).toMatchObject({
      amount: '1200.50',
      fee: '',
      status: '待确认',
      fundCode: '510300',
    });
    expect(saved.conditions.cash).toBe('');
  });
  it('保存失败保留条件输入且不伪造保存成功', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...initialState, scenario: 'error' }));
    const user = userEvent.setup();
    renderPage('/portfolio?tab=conditions');
    await user.type(screen.getByLabelText('已知可用现金（元）'), '1500.00');
    await user.click(screen.getByRole('button', { name: '保存投资条件' }));
    expect(screen.getByLabelText('已知可用现金（元）')).toHaveValue('1500.00');
    expect(screen.getByRole('status')).toHaveTextContent('演示保存失败');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).conditions.cash).toBe('');
  });
});
