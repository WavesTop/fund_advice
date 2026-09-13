import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { DemoProvider, STORAGE_KEY } from '../../shared/store';
import { initialState } from '../../shared/data';
import { ReviewPage } from './ReviewPage';

vi.mock('../../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));
beforeEach(() => {
  localStorage.clear();
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const renderPage = (tab: string) =>
  render(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: '/review',
            element: (
              <DemoProvider>
                <ReviewPage />
              </DemoProvider>
            ),
          },
        ],
        { initialEntries: [`/review?tab=${tab}`] },
      )}
    />,
  );

describe('review interactions', () => {
  it('requires explicit confirmation to adopt and restore while preserving both events', async () => {
    const user = userEvent.setup();
    renderPage('strategy');
    await user.click(screen.getByRole('button', { name: '演示启用 v1.1' }));
    let dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: '确认启用' })).toBeDisabled();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    await user.click(within(dialog).getByLabelText('我已查看变更与验证局限，确认本次演示操作'));
    await user.click(within(dialog).getByRole('button', { name: '确认启用' }));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).strategyVersion).toBe('v1.1');
    await user.click(screen.getByRole('button', { name: '演示恢复 v1.0' }));
    dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByLabelText('我已查看变更与验证局限，确认本次演示操作'));
    await user.click(within(dialog).getByRole('button', { name: '确认恢复' }));
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(saved.strategyVersion).toBe('v1.0');
    expect(saved.strategyHistory.map((event: { version: string }) => event.version)).toEqual([
      'v1.1',
      'v1.0',
    ]);
  });

  it('keeps unsaved review notes when persistence fails', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...initialState, scenario: 'error' }));
    const user = userEvent.setup();
    renderPage('timeline');
    await user.type(screen.getByLabelText('复盘笔记'), '操作时点需要核对');
    await user.click(screen.getByRole('button', { name: '保存笔记' }));
    expect(screen.getByLabelText('复盘笔记')).toHaveValue('操作时点需要核对');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).reviewNotes).toBe('');
    expect(screen.getByRole('status')).toHaveTextContent('保存失败');
  });

  it('shows unavailable personal returns instead of assigning the samples to the user', async () => {
    const user = userEvent.setup();
    renderPage('comparison');
    expect(screen.getByText('我的实际收益').parentElement).toHaveTextContent('—');
    await user.click(screen.getByLabelText('展示虚构比较样例'));
    expect(screen.getByText('还没有可核算的收益结果')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('retains three period summaries and suppresses direct differences when periods differ', async () => {
    const user = userEvent.setup();
    renderPage('comparison');
    await user.selectOptions(screen.getByLabelText('比较条件'), 'different');
    expect(screen.getAllByRole('img')).toHaveLength(3);
    expect(screen.getAllByRole('button', { name: '暂不可比' })).toHaveLength(3);
    for (const button of screen.getAllByRole('button', { name: '暂不可比' }))
      expect(button).toBeDisabled();
    for (const name of ['短期模拟', '中期模拟', '长期模拟'])
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
  });
});
