import { useEffect, useState } from 'react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import type { SectorOpportunity } from './SectorOpportunities';

type History = {
  code: string;
  source_id: string;
  universe_type: string;
  history_source_id: string;
  history_source_code: string;
  history_relation: string;
  as_of: string | null;
  collection_error: string | null;
  rows: Array<{ date: string; open: string; high: string; low: string; close: string }>;
};

export function SectorHistory({ item }: { item: SectorOpportunity }) {
  const [open, setOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setError('');
    setData(null);
    const universe = item.universe_type ?? 'tracked_index';
    const params = new URLSearchParams({ source_id: item.source_id, universe_type: universe });
    fetch(`/api/sectors/${encodeURIComponent(item.code)}/series?${params}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error('行情暂不可用');
        return (await response.json()) as History;
      })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (
          result.code !== item.code ||
          result.source_id !== item.source_id ||
          result.universe_type !== universe ||
          !Array.isArray(result.rows)
        )
          throw new Error('行情身份与请求不一致');
        const dates = new Set<string>();
        for (const row of result.rows) {
          const values = [row.open, row.high, row.low, row.close].map(Number);
          if (
            !/^\d{4}-\d{2}-\d{2}$/.test(row.date) ||
            dates.has(row.date) ||
            values.some((value) => !Number.isFinite(value) || value <= 0) ||
            values[2] > Math.min(values[0], values[3]) ||
            values[1] < Math.max(values[0], values[3])
          )
            throw new Error('行情字段无效');
          dates.add(row.date);
        }
        setData(result);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : '行情加载失败');
      });
    return () => controller.abort();
  }, [open, retry, item.code, item.source_id, item.universe_type]);
  const option: EChartsOption = {
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: { left: 58, right: 20, top: 24, bottom: 60 },
    xAxis: { type: 'category', data: data?.rows.map((row) => row.date) ?? [] },
    yAxis: { type: 'value', scale: true, name: '点位' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 4, height: 20 }],
    series: [
      {
        type: 'candlestick',
        name: '来源日线',
        data:
          data?.rows.map((row) => [
            Number(row.open),
            Number(row.close),
            Number(row.low),
            Number(row.high),
          ]) ?? [],
      },
    ],
  };
  return (
    <section>
      <button className="button secondary" onClick={() => setOpen((value) => !value)}>
        {open ? '收起真实日线' : '查看真实日线'}
      </button>
      {open && (
        <>
          {error ? (
            <p role="alert">
              {error} <button onClick={() => setRetry((value) => value + 1)}>重试行情</button>
            </p>
          ) : !data ? (
            <p role="status">正在读取该来源的行情…</p>
          ) : (
            <>
              <p>
                行情截至 {data.as_of || '暂无'} · {data.history_source_id} /{' '}
                {data.history_source_code}
              </p>
              {data.history_relation === 'proxy_not_equivalent' && (
                <p>跨源参考行情：未证明成分、权重或编制方法等价，不用于持仓归因或基金映射。</p>
              )}
              {data.collection_error && (
                <p role="alert">更新失败，以下保留历史行情供核对：{data.collection_error}</p>
              )}
              {data.rows.length ? (
                <Chart option={option} label={`${item.code}来源真实日线`} height={340} />
              ) : (
                <p>该区间没有可用日线。</p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
