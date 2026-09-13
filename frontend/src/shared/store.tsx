import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useBlocker } from 'react-router-dom';
import { initialState } from './data';
import { parseDemoState } from './demo-serialization';
import type { DemoState } from './types';

export const STORAGE_KEY = 'fund-advice.demo.v1';
export function readDemoState(storage: Pick<Storage, 'getItem'>): {
  state: DemoState;
  warning?: string;
} {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return { state: structuredClone(initialState) };
    return { state: parseDemoState(JSON.parse(raw)) };
  } catch {
    return {
      state: structuredClone(initialState),
      warning:
        '演示草稿无法读取。请先检查或备份浏览器原始存储；新的保存会替换原草稿。当前显示初始演示界面。',
    };
  }
}

interface DemoContextValue {
  state: DemoState;
  update: (change: (previous: DemoState) => DemoState) => boolean;
  notify: (message: string) => void;
  enqueueTask: (name: string) => string;
  resetDemo: () => void;
  registerUnsavedForm: (id: symbol) => () => void;
}
const DemoContext = createContext<DemoContextValue | null>(null);

function UnsavedNavigationGuard({ hasUnsavedForms }: { hasUnsavedForms: () => boolean }) {
  // React Router supports one blocker. All open forms share this single guard.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      hasUnsavedForms() &&
      `${currentLocation.pathname}${currentLocation.search}` !==
        `${nextLocation.pathname}${nextLocation.search}`,
  );
  useEffect(() => {
    if (blocker.state === 'blocked') {
      if (!hasUnsavedForms() || window.confirm('还有未保存的内容，确定放弃并离开吗？'))
        blocker.proceed();
      else blocker.reset();
    }
  }, [blocker, hasUnsavedForms]);
  useEffect(() => {
    const preventLoss = (event: BeforeUnloadEvent) => {
      if (hasUnsavedForms()) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', preventLoss);
    return () => window.removeEventListener('beforeunload', preventLoss);
  }, [hasUnsavedForms]);
  return null;
}

export function DemoProvider({ children }: { children: ReactNode }) {
  const unsavedForms = useRef(new Set<symbol>());
  const [unsavedCount, setUnsavedCount] = useState(0);
  const hasUnsavedForms = useCallback(() => unsavedForms.current.size > 0, []);
  const registerUnsavedForm = useCallback((id: symbol) => {
    unsavedForms.current.add(id);
    setUnsavedCount(unsavedForms.current.size);
    return () => {
      unsavedForms.current.delete(id);
      setUnsavedCount(unsavedForms.current.size);
    };
  }, []);
  const [loaded] = useState(() => {
    try {
      return readDemoState(window.localStorage);
    } catch {
      return {
        state: structuredClone(initialState),
        warning: '浏览器存储不可访问，仍可浏览初始演示资料。',
      };
    }
  });
  const [state, setState] = useState(loaded.state);
  const current = useRef(state);
  const [message, setMessage] = useState(loaded.warning ?? '');
  const notify = useCallback((text: string) => setMessage(text), []);
  const update = useCallback((change: (previous: DemoState) => DemoState) => {
    const next = change(structuredClone(current.current));
    if (current.current.scenario === 'error' && next.scenario === 'error') {
      setMessage('演示保存失败：输入已保留。可在设置中切回正常场景后重试。');
      return false;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      current.current = next;
      setState(next);
      return true;
    } catch {
      setMessage(
        '浏览器无法保存演示草稿，可能是存储空间不足或访问受限。输入已保留，请检查后重试。',
      );
      return false;
    }
  }, []);
  const enqueueTask = useCallback(
    (name: string) => {
      const existing = current.current.tasks.find(
        (task) => task.name === name && task.status === '排队中',
      );
      if (existing) {
        notify('该演示任务已在队列中，可在设置的任务记录中查看。');
        return existing.id;
      }
      const id = crypto.randomUUID();
      const saved = update((previous) => ({
        ...previous,
        tasks: [
          ...previous.tasks,
          {
            id,
            name,
            status: '排队中',
            createdAt: new Date().toISOString(),
            detail: '演示队列：后台服务尚未接入，不会执行实际采集、计算或外部调用。',
          },
        ],
      }));
      if (saved) notify('已加入演示队列。实际执行待后台服务接入。');
      return saved ? id : '';
    },
    [update, notify],
  );
  const resetDemo = useCallback(() => {
    if (update(() => structuredClone(initialState))) notify('演示记录已重置。');
  }, [update, notify]);
  return (
    <DemoContext.Provider
      value={{ state, update, notify, enqueueTask, resetDemo, registerUnsavedForm }}
    >
      {unsavedCount > 0 && <UnsavedNavigationGuard hasUnsavedForms={hasUnsavedForms} />}
      {children}
      {message && (
        <div className="toast" role="status">
          <span>{message}</span>
          <button type="button" aria-label="关闭提示" onClick={() => setMessage('')}>
            ×
          </button>
        </div>
      )}
    </DemoContext.Provider>
  );
}
export function useDemo() {
  const context = useContext(DemoContext);
  if (!context) throw new Error('useDemo must be used inside DemoProvider');
  return context;
}
export function useUnsavedChanges(dirty: boolean) {
  const { registerUnsavedForm } = useDemo();
  const id = useRef(Symbol('unsaved-form'));
  useEffect(() => {
    if (dirty) return registerUnsavedForm(id.current);
  }, [dirty, registerUnsavedForm]);
}
