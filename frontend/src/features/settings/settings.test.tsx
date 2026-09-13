import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { DemoProvider, STORAGE_KEY } from '../../shared/store';
import { initialState } from '../../shared/data';
import { createDemoExport } from './importDemo';
import { SettingsPage } from './SettingsPage';

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
const renderPage = (tab = 'data') =>
  render(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: '/settings',
            element: (
              <DemoProvider>
                <SettingsPage />
              </DemoProvider>
            ),
          },
        ],
        { initialEntries: [`/settings?tab=${tab}`] },
      )}
    />,
  );
function demoFile(content: string) {
  const file = new File([content], 'demo.json', { type: 'application/json' });
  Object.defineProperty(file, 'text', { value: async () => content });
  return file;
}

describe('settings interaction boundaries', () => {
  it('leaves existing records intact when an import is invalid', async () => {
    const existing = { ...initialState, reviewNotes: '当前内容不能丢失' };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(existing));
    const user = userEvent.setup();
    renderPage('local');
    await user.upload(screen.getByLabelText('选择演示记录 JSON 文件'), demoFile('{invalid'));
    expect(await screen.findByRole('alert')).toHaveTextContent('导入未执行');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).reviewNotes).toBe('当前内容不能丢失');
  });

  it('requires reviewing and confirming a valid import before replacing records', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...initialState, reviewNotes: '原笔记' }));
    const user = userEvent.setup();
    renderPage('local');
    await user.upload(
      screen.getByLabelText('选择演示记录 JSON 文件'),
      demoFile(createDemoExport({ ...initialState, reviewNotes: '导入的笔记' })),
    );
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: '确认导入' })).toBeDisabled();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).reviewNotes).toBe('原笔记');
    await user.click(within(dialog).getByLabelText('我已核对范围，并理解将替换当前演示记录'));
    await user.click(within(dialog).getByRole('button', { name: '确认导入' }));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).reviewNotes).toBe('导入的笔记');
    expect(screen.getByRole('status')).toHaveTextContent('未恢复或改变实际数据库');
  });

  it('retains a failed attempt, deduplicates retry, and preserves cancelled history', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: '更新全部资料' }));
    await user.click(screen.getByRole('button', { name: '任务记录' }));
    await user.click(screen.getByRole('button', { name: '查看详情' }));
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: '演示任务失败' }),
    );
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '完成查看' }));
    await user.click(screen.getByRole('button', { name: '重试' }));
    await user.click(screen.getByRole('button', { name: '重试' }));
    let state = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(state.tasks.map((task: { status: string }) => task.status)).toEqual(['失败', '排队中']);
    await user.click(screen.getByRole('button', { name: '取消' }));
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: '确认取消任务' }),
    );
    state = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(state.tasks.map((task: { status: string }) => task.status)).toEqual(['失败', '已取消']);
  });

  it('previews AI scope without accepting a secret, saving a configuration, or making a request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderPage('ai');
    expect(screen.getByLabelText('API 密钥')).toBeDisabled();
    expect(screen.getByRole('button', { name: '检查连接 · 待接入' })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('服务类型'), '兼容接口');
    await user.type(screen.getByLabelText('模型名称'), 'a-model');
    await user.type(screen.getByLabelText('接口地址 · 不含密钥'), 'https://example.com/v1');
    await user.click(screen.getByLabelText('个人持仓与交易摘要'));
    await user.click(screen.getByRole('button', { name: '预览发送范围' }));
    expect(
      within(screen.getByRole('dialog')).getByText('计划包含，正式启用前需确认'),
    ).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).getByText('未发送')).toBeInTheDocument();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
