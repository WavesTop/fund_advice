import { useCallback, useEffect, useRef, useState } from 'react';
import { parseFundIdentity, parseResearchResponse, type FundIdentity, type ResearchResponse } from './research-model';

type Result = { research: ResearchResponse; fund: FundIdentity | null };
type LoadState = { key: string; result: Result | null; loading: boolean; error: string; notFound: boolean };
class MissingFund extends Error {}

export function useResearchData(fundCode: string) {
  const request = useRef<AbortController | null>(null);
  const [state, setState] = useState<LoadState>({ key: fundCode, result: null, loading: true, error: '', notFound: false });
  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState((previous) => ({ key: fundCode, result: previous.key === fundCode ? previous.result : null, loading: true, error: '', notFound: false }));
    const fetchJson = async (path: string, fund = false): Promise<unknown> => {
      const response = await fetch(path, { signal: controller.signal });
      if (fund && response.status === 404) throw new MissingFund(`本地真实目录没有基金 ${fundCode}，未切换到示例基金。`);
      if (!response.ok) throw new Error(`读取本地资料失败（HTTP ${response.status}）。请确认数据服务已启动后重试。`);
      return response.json();
    };
    const run = async (): Promise<Result> => {
      if (fundCode && !/^\d{6}$/.test(fundCode)) throw new Error(`基金代码 ${fundCode} 无效，未替换对象。`);
      const [research, fund] = await Promise.all([
        fetchJson('/api/sectors/opportunities').then(parseResearchResponse),
        fundCode ? fetchJson(`/api/funds/${encodeURIComponent(fundCode)}`, true).then((raw) => parseFundIdentity(raw, fundCode)) : Promise.resolve(null),
      ]);
      return { research, fund };
    };
    void run().then((result) => {
      if (!controller.signal.aborted) setState({ key: fundCode, result, loading: false, error: '', notFound: false });
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setState((previous) => ({ key: fundCode,
        result: cause instanceof MissingFund ? null : previous.key === fundCode ? previous.result : null,
        loading: false, notFound: cause instanceof MissingFund,
        error: cause instanceof Error ? cause.message : '研究读取失败，未填入默认结果。' }));
    });
  }, [fundCode]);
  useEffect(() => {
    load();
    return () => request.current?.abort();
  }, [load]);
  // Effects run after render: hide the prior identity immediately on a route change.
  const visible = state.key === fundCode ? state : { key: fundCode, result: null, loading: true, error: '', notFound: false };
  return { ...visible, reload: load };
}
