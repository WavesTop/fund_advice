import { act, renderHook, cleanup, render, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DemoProvider, readDemoState, STORAGE_KEY, useDemo, useUnsavedChanges } from './store';
import { initialState } from './data';
import { useState } from 'react';
import { createMemoryRouter, Link, Outlet, RouterProvider } from 'react-router-dom';
import { Modal } from './ui';

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe('persistent demo interactions', () => {
  it('restores a saved draft after mounting a new provider', () => {
    const first = renderHook(() => useDemo(), { wrapper: DemoProvider });
    act(() => {
      first.result.current.update((previous) => ({ ...previous, reviewNotes: '保留当时理由' }));
    });
    first.unmount();
    const next = renderHook(() => useDemo(), { wrapper: DemoProvider });
    expect(next.result.current.state.reviewNotes).toBe('保留当时理由');
  });
  it('rejects a failed storage write without displaying unsaved state as saved', () => {
    const { result } = renderHook(() => useDemo(), { wrapper: DemoProvider });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota', 'QuotaExceededError');
    });
    let saved: boolean | undefined;
    act(() => {
      saved = result.current.update((previous) => ({ ...previous, reviewNotes: '不能丢失的输入' }));
    });
    expect(saved).toBe(false);
    expect(result.current.state.reviewNotes).toBe('');
  });
  it('deduplicates rapid task submissions without claiming execution succeeded', () => {
    const { result } = renderHook(() => useDemo(), { wrapper: DemoProvider });
    let first = '';
    let second = '';
    act(() => {
      first = result.current.enqueueTask('更新示例目录');
      second = result.current.enqueueTask('更新示例目录');
    });
    expect(second).toBe(first);
    expect(result.current.state.tasks).toHaveLength(1);
    expect(result.current.state.tasks[0].status).toBe('排队中');
  });
  it('allows recovery from the failing scenario without writing failed changes', () => {
    const { result } = renderHook(() => useDemo(), { wrapper: DemoProvider });
    act(() => {
      result.current.update((previous) => ({ ...previous, scenario: 'error' }));
    });
    let saved = true;
    act(() => {
      saved = result.current.update((previous) => ({ ...previous, reviewNotes: '失败写入' }));
    });
    expect(saved).toBe(false);
    act(() => {
      result.current.update((previous) => ({ ...previous, scenario: 'ready' }));
    });
    expect(result.current.state.scenario).toBe('ready');
    expect(result.current.state.reviewNotes).toBe('');
  });
});
describe('loading demo storage', () => {
  it('returns an explicit warning and leaves damaged storage untouched', () => {
    localStorage.setItem(STORAGE_KEY, '{broken');
    const result = readDemoState(localStorage);
    expect(result.state).toEqual(initialState);
    expect(result.warning).toContain('无法读取');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('{broken');
  });
  it('can show initial UI even when the browser denies reading storage', () => {
    const result = readDemoState({
      getItem() {
        throw new Error('denied');
      },
    });
    expect(result.warning).toBeTruthy();
    expect(result.state.transactions).toEqual([]);
  });
});

function UnsavedEditor() {
  const [note, setNote] = useState('');
  const [saved, setSaved] = useState('');
  const [open, setOpen] = useState(false);
  const [modalNote, setModalNote] = useState('');
  useUnsavedChanges(note !== saved);
  return (
    <>
      <label>
        页面草稿
        <input value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      <button onClick={() => setSaved(note)}>保存页面草稿</button>
      <button onClick={() => setOpen(true)}>打开弹窗</button>
      <Link to="/target">离开编辑页面</Link>
      {open && (
        <Modal title="弹窗草稿" dirty={!!modalNote} onClose={() => setOpen(false)}>
          <label>
            弹窗输入
            <input value={modalNote} onChange={(event) => setModalNote(event.target.value)} />
          </label>
          <Link to="/target">弹窗内导航</Link>
        </Modal>
      )}
    </>
  );
}

function renderGuardEditor() {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute('open');
  };
  const router = createMemoryRouter(
    [
      {
        element: (
          <DemoProvider>
            <Outlet />
          </DemoProvider>
        ),
        children: [
          { path: '/editor', element: <UnsavedEditor /> },
          { path: '/target', element: <h1>目标页面</h1> },
        ],
      },
    ],
    { initialEntries: ['/editor'] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe('shared unsaved navigation guard', () => {
  it('leaves clean pages and read-only dialogs without registering a blocker', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const router = renderGuardEditor();
    fireEvent.click(screen.getByRole('button', { name: '打开弹窗' }));
    fireEvent.click(screen.getByRole('link', { name: '弹窗内导航' }));
    expect(router.state.location.pathname).toBe('/target');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('retains modal input after cancelled SPA navigation and leaves after confirmation', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const router = renderGuardEditor();
    fireEvent.click(screen.getByRole('button', { name: '打开弹窗' }));
    fireEvent.change(screen.getByLabelText('弹窗输入'), { target: { value: '不能丢失' } });
    fireEvent.click(screen.getByRole('link', { name: '弹窗内导航' }));
    expect(router.state.location.pathname).toBe('/editor');
    expect(screen.getByLabelText('弹窗输入')).toHaveValue('不能丢失');
    expect(confirm).toHaveBeenCalledTimes(1);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('link', { name: '弹窗内导航' }));
    expect(router.state.location.pathname).toBe('/target');
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it('keeps the underlying dirty form protected after closing a modal and clears after save', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const warning = vi.spyOn(console, 'warn');
    const router = renderGuardEditor();
    fireEvent.change(screen.getByLabelText('页面草稿'), { target: { value: '页面内容' } });
    fireEvent.click(screen.getByRole('button', { name: '打开弹窗' }));
    fireEvent.change(screen.getByLabelText('弹窗输入'), { target: { value: '弹窗内容' } });
    confirm.mockReturnValue(false);
    fireEvent.click(screen.getByRole('link', { name: '弹窗内导航' }));
    expect(router.state.location.pathname).toBe('/editor');
    expect(confirm).toHaveBeenCalledTimes(1);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('页面草稿')).toHaveValue('页面内容');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    confirm.mockReturnValue(false);
    fireEvent.click(screen.getByRole('link', { name: '离开编辑页面' }));
    expect(router.state.location.pathname).toBe('/editor');
    fireEvent.click(screen.getByRole('button', { name: '保存页面草稿' }));
    const cleanUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
    fireEvent.click(screen.getByRole('link', { name: '离开编辑页面' }));
    expect(router.state.location.pathname).toBe('/target');
    expect(confirm).toHaveBeenCalledTimes(3);
    expect(
      warning.mock.calls
        .flat()
        .some((value) => String(value).includes('only supports one blocker')),
    ).toBe(false);
  });
});
