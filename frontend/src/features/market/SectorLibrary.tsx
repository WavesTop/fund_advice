import { useEffect, useMemo, useRef, useState } from 'react';
import type { EChartsOption } from 'echarts';
import { Badge, EmptyState, Notice, Panel, searchInputProps } from '../../shared/ui';
import { Chart } from '../../shared/Chart';
import { sourceLabel } from '../../shared/source-label';
import { priceDisplay } from './sector-history-display';
import { parseSectorEvaluation } from './sector-evaluation-model';
import { readSectorCache, sectorCacheRevision, writeSectorCache } from './sector-request-state';
import { subjectKey } from './real-market-support';
import {
  calculateTechnical,
  detectTechnicalStages,
  technicalStageLabels,
  type PriceRow,
  type TechnicalPoint,
  type TechnicalStage,
} from './sector-technical';
import { type OpportunitiesResponse, type SectorOpportunity } from './SectorOpportunities';
import './sector-opportunities.css';

type History = {
  code: string;
  source_id: string;
  universe_type: string;
  as_of: string | null;
  history_source_id: string;
  history_source_code: string;
  collection_error: string | null;
  rows: PriceRow[];
};

type ScanResult = {
  asOf: string | null;
  stages: TechnicalStage[];
  points: TechnicalPoint[];
  error?: string;
};

const stageOptions: Array<{ value: '' | TechnicalStage; label: string }> = [
  { value: '', label: '全部阶段' },
  { value: 'underwater-golden-cross', label: technicalStageLabels['underwater-golden-cross'] },
  { value: 'yellow-near-zero', label: technicalStageLabels['yellow-near-zero'] },
  { value: 'white-near-zero', label: technicalStageLabels['white-near-zero'] },
  { value: 'above-zero-death-cross', label: technicalStageLabels['above-zero-death-cross'] },
];

function formatDate(value: string | null | undefined) {
  return value?.slice(0, 10) || '暂无';
}

function formatPercent(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function numberText(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits);
}

function latest(point: TechnicalPoint[] | undefined) {
  return point?.at(-1) ?? null;
}

function historyUrl(item: SectorOpportunity) {
  const universe = item.universe_type ?? 'hot_board';
  const params = new URLSearchParams({ source_id: item.source_id, universe_type: universe });
  return `/api/sectors/${encodeURIComponent(item.code)}/series?${params}`;
}

function klineOption(item: SectorOpportunity, result: ScanResult): EChartsOption {
  const rows = result.points;
  const dates = rows.map((row) => row.date);
  const grid = [
    { left: 60, right: 28, top: 28, height: 190 },
    { left: 60, right: 28, top: 258, height: 95 },
    { left: 60, right: 28, top: 394, height: 95 },
    { left: 60, right: 28, top: 530, height: 80 },
    { left: 60, right: 28, top: 650, height: 80 },
  ];
  const axisBase = (gridIndex: number) => ({
    type: 'category' as const,
    gridIndex,
    data: dates,
    boundaryGap: true,
    axisLabel: {
      show: gridIndex === 4,
      color: '#637684',
      hideOverlap: true,
      formatter: (value: string) => value.slice(5),
    },
    axisTick: { show: false },
    axisLine: { lineStyle: { color: '#dbe4ed' } },
  });
  return {
    animation: false,
    color: ['#326a79', '#b18a24', '#7a5aa6', '#bf4752', '#26806b', '#4c78a8'],
    grid,
    legend: {
      top: 0,
      right: 24,
      itemWidth: 10,
      itemHeight: 8,
      textStyle: { color: '#526774', fontSize: 11 },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      confine: true,
      renderMode: 'richText',
    },
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2, 3, 4], start: 45, end: 100, minValueSpan: 20 },
      {
        type: 'slider',
        xAxisIndex: [0, 1, 2, 3, 4],
        bottom: 8,
        height: 22,
        left: 60,
        right: 28,
        start: 45,
        end: 100,
        showDetail: false,
        brushSelect: false,
      },
    ],
    xAxis: [0, 1, 2, 3, 4].map(axisBase),
    yAxis: [
      {
        type: 'value',
        scale: true,
        gridIndex: 0,
        name: 'K线',
        axisLabel: { color: '#637684' },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
      {
        type: 'value',
        scale: true,
        gridIndex: 1,
        name: 'MACD',
        axisLabel: { color: '#637684' },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
      {
        type: 'value',
        min: 0,
        max: 100,
        gridIndex: 2,
        name: 'KDJ',
        axisLabel: { color: '#637684' },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
      {
        type: 'value',
        min: 0,
        max: 100,
        gridIndex: 3,
        name: 'RSI',
        axisLabel: { color: '#637684' },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
      {
        type: 'value',
        min: 0,
        max: 100,
        gridIndex: 4,
        name: 'WR',
        axisLabel: { color: '#637684' },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: rows.map((row) => [row.open, row.close, row.low, row.high]),
        itemStyle: { color: '#bf4752', color0: '#26806b', borderColor: '#bf4752', borderColor0: '#26806b' },
        barMaxWidth: 12,
      },
      {
        name: 'MACD柱',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => ({
          value: row.macd,
          itemStyle: { color: (row.macd ?? 0) >= 0 ? '#bf4752' : '#26806b', opacity: 0.7 },
        })),
      },
      {
        name: '白线DIF',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => row.dif),
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#f7fbff' },
        markLine: { symbol: 'none', data: [{ yAxis: 0 }], lineStyle: { color: '#9baab5', type: 'dashed' }, label: { show: false } },
      },
      {
        name: '黄线DEA',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => row.dea),
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#c59b32' },
      },
      {
        name: 'K',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: rows.map((row) => row.k),
        showSymbol: false,
      },
      {
        name: 'D',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: rows.map((row) => row.d),
        showSymbol: false,
      },
      {
        name: 'J',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: rows.map((row) => row.j),
        showSymbol: false,
      },
      {
        name: 'RSI6',
        type: 'line',
        xAxisIndex: 3,
        yAxisIndex: 3,
        data: rows.map((row) => row.rsi6),
        showSymbol: false,
      },
      {
        name: 'RSI12',
        type: 'line',
        xAxisIndex: 3,
        yAxisIndex: 3,
        data: rows.map((row) => row.rsi12),
        showSymbol: false,
      },
      {
        name: 'RSI24',
        type: 'line',
        xAxisIndex: 3,
        yAxisIndex: 3,
        data: rows.map((row) => row.rsi24),
        showSymbol: false,
      },
      {
        name: 'WR6',
        type: 'line',
        xAxisIndex: 4,
        yAxisIndex: 4,
        data: rows.map((row) => row.wr6),
        showSymbol: false,
      },
      {
        name: 'WR10',
        type: 'line',
        xAxisIndex: 4,
        yAxisIndex: 4,
        data: rows.map((row) => row.wr10),
        showSymbol: false,
      },
    ],
    title: {
      text: `${item.name} · ${item.code}`,
      left: 60,
      top: 0,
      textStyle: { color: '#263d47', fontSize: 13, fontWeight: 600 },
    },
  };
}

function StageBadges({ stages }: { stages: TechnicalStage[] }) {
  if (!stages.length) return <span className="muted">暂无命中</span>;
  return (
    <>
      {stages.map((stage) => (
        <Badge key={stage} tone={stage === 'above-zero-death-cross' ? 'amber' : 'blue'}>
          {technicalStageLabels[stage]}
        </Badge>
      ))}
    </>
  );
}

export function SectorLibrary() {
  const [data, setData] = useState<OpportunitiesResponse | null>(readSectorCache);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>(() => readSectorCache() ? 'ready' : 'loading');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [stage, setStage] = useState<'' | TechnicalStage>('');
  const [selectedKey, setSelectedKey] = useState('');
  const [scan, setScan] = useState({ done: 0, total: 0, running: false });
  const [technical, setTechnical] = useState<Record<string, ScanResult>>({});
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const revision = sectorCacheRevision();
    if (!data) setState('loading');
    fetch('/api/sectors/opportunities', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return parseSectorEvaluation(await response.json());
      })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (revision === sectorCacheRevision()) writeSectorCache(result);
        setData(result);
        setState('ready');
        setError('');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : '板块库读取失败。');
        setState(data ? 'ready' : 'error');
      });
    return () => controller.abort();
  }, []);

  const boards = useMemo(
    () =>
      (data?.items ?? [])
        .filter((item) => item.universe_type === 'hot_board')
        .sort((a, b) => (a.heat_rank ?? 9999) - (b.heat_rank ?? 9999) || a.code.localeCompare(b.code)),
    [data],
  );
  const boardKey = boards.map((item) => `${subjectKey(item)}:${item.updated_at}:${item.as_of}`).join('|');

  useEffect(() => {
    if (!boards.length) return;
    setSelectedKey((value) => value || subjectKey(boards[0]));
  }, [boards]);

  useEffect(() => {
    if (!boards.length) return;
    let cancelled = false;
    const controllers: AbortController[] = [];
    setTechnical({});
    setScan({ done: 0, total: boards.length, running: true });
    let cursor = 0;
    const worker = async () => {
      while (!cancelled && cursor < boards.length) {
        const item = boards[cursor];
        cursor += 1;
        const controller = new AbortController();
        controllers.push(controller);
        const key = subjectKey(item);
        try {
          const response = await fetch(historyUrl(item), { signal: controller.signal });
          if (!response.ok) throw new Error('日线暂不可用');
          const history = (await response.json()) as History;
          if (history.code !== item.code || history.source_id !== item.source_id) throw new Error('日线身份不一致');
          const points = calculateTechnical(history.rows);
          const stages = detectTechnicalStages(points);
          if (!cancelled) {
            setTechnical((values) => ({ ...values, [key]: { asOf: history.as_of, stages, points, error: history.collection_error || undefined } }));
          }
        } catch (cause) {
          if (!cancelled && !(cause instanceof DOMException && cause.name === 'AbortError')) {
            setTechnical((values) => ({ ...values, [key]: { asOf: null, stages: [], points: [], error: cause instanceof Error ? cause.message : '日线加载失败' } }));
          }
        } finally {
          if (!cancelled) setScan((value) => ({ ...value, done: Math.min(value.done + 1, value.total) }));
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(6, boards.length) }, worker)).finally(() => {
      if (!cancelled) setScan((value) => ({ ...value, running: false }));
    });
    return () => {
      cancelled = true;
      controllers.forEach((controller) => controller.abort());
    };
  }, [boardKey]);

  const normalized = query.trim().toLowerCase();
  const filtered = boards.filter((item) => {
    const key = subjectKey(item);
    if (stage && !technical[key]?.stages.includes(stage)) return false;
    return !normalized || item.name.toLowerCase().includes(normalized) || item.code.toLowerCase().includes(normalized);
  });
  const selected = boards.find((item) => subjectKey(item) === selectedKey) ?? filtered[0] ?? boards[0];
  const selectedResult = selected ? technical[subjectKey(selected)] : undefined;
  const current = latest(selectedResult?.points);
  const short = selected?.periods.find((period) => period.id === 'short');
  const shortDisplay = short ? priceDisplay(short, Boolean(selected.collection_error)) : null;
  const matchedCount = stage ? boards.filter((item) => technical[subjectKey(item)]?.stages.includes(stage)).length : boards.length;

  return (
    <div className="sector-library">
      <Notice title="板块库 · K线与常用指标图" tone="info">
        当前展示本地已采集行业板块的真实日线，并按 MACD 白线 DIF、黄线 DEA 与零轴关系标注阶段。技术形态仅用于观察，不生成买卖建议。
      </Notice>
      <div className="sector-library-toolbar">
        <label>
          搜索板块
          <input
            {...searchInputProps}
            aria-label="搜索板块库名称或代码"
            placeholder="名称或代码"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label>
          阶段筛选
          <select aria-label="技术阶段筛选" value={stage} onChange={(event) => setStage(event.target.value as '' | TechnicalStage)}>
            {stageOptions.map((option) => <option key={option.value || 'all'} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <div className="sector-library-count">
          <strong>{boards.length}</strong> 个板块 · 已扫描 {scan.done}/{scan.total}
          {scan.running ? ' · 扫描中' : ''}
          {stage && <> · 命中 {matchedCount}</>}
        </div>
      </div>
      {error && <p role="alert" className="market-error">{error}</p>}
      {state === 'loading' && <p role="status" className="sector-state">正在读取板块库…</p>}
      {state === 'error' && <EmptyState title="板块库暂时无法加载" description={error || '请确认本地数据服务已启动。'} />}
      {state === 'ready' && !boards.length && <EmptyState title="暂无板块库资料" description="本地行业板块池尚未采集成功。" />}
      {state === 'ready' && !!boards.length && (
        <div className="sector-library-layout">
          <Panel className="sector-library-list" title="板块列表" subtitle="按真实行业池展示，点击查看右侧指标图。">
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>板块</th>
                    <th>行情</th>
                    <th>近1月</th>
                    <th>技术阶段</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((item) => {
                    const key = subjectKey(item);
                    const result = technical[key];
                    const display = item.periods.find((period) => period.id === 'short');
                    const change = display ? priceDisplay(display, Boolean(item.collection_error)).change : null;
                    return (
                      <tr key={key} className={selected && key === subjectKey(selected) ? 'sector-library-selected' : ''}>
                        <td>
                          <button type="button" className="button-link" onClick={() => setSelectedKey(key)}>
                            <strong>{item.name}</strong>
                            <small>{item.code} · 热度 #{item.heat_rank ?? '—'}</small>
                          </button>
                        </td>
                        <td>{formatDate(result?.asOf ?? item.as_of)}<small>{sourceLabel(item.history_source_id ?? item.source_id)}</small></td>
                        <td>{formatPercent(change)}</td>
                        <td className="sector-library-stages">
                          {result ? result.error ? <span className="muted">{result.error}</span> : <StageBadges stages={result.stages} /> : <span className="muted">待扫描</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!filtered.length && <p className="research-empty">没有匹配的板块。扫描未完成时，技术阶段筛选可能暂时为空。</p>}
          </Panel>
          <Panel
            className="sector-library-chart"
            title={selected ? `${selected.name} · ${selected.code}` : '指标图'}
            subtitle={selected ? `${sourceLabel(selected.history_source_id ?? selected.source_id)} · 行情截至 ${formatDate(selectedResult?.asOf ?? selected.as_of)}` : '请选择一个板块'}
          >
            {selected && (
              <div className="sector-library-summary">
                <span>近1月 {formatPercent(shortDisplay?.change)}</span>
                <span>收盘 {numberText(current?.close, 2)}</span>
                <span>白线DIF {numberText(current?.dif, 3)}</span>
                <span>黄线DEA {numberText(current?.dea, 3)}</span>
                <span>MACD柱 {numberText(current?.macd, 3)}</span>
              </div>
            )}
            {!selected ? (
              <EmptyState title="请选择板块" description="左侧选择一个板块后显示 K 线和指标图。" />
            ) : !selectedResult ? (
              <p role="status" className="sector-state">正在读取该板块日线并计算指标…</p>
            ) : selectedResult.points.length < 35 ? (
              <EmptyState title="指标图暂不可用" description={selectedResult.error || '日线样本不足，无法计算 MACD、KDJ、RSI 与 WR。'} />
            ) : (
              <>
                <div className="sector-library-stages sector-library-current">
                  <StageBadges stages={selectedResult.stages} />
                </div>
                <Chart
                  option={klineOption(selected, selectedResult)}
                  label={`${selected.name} K线以及 MACD、KDJ、RSI、WR 四类常用指标图`}
                  height={760}
                />
                <p className="muted">
                  MACD 白线为 DIF，黄线为 DEA；“零轴附近”按最新收盘价的 0.3% 以内判断。WR 数值越低越接近近期高位，越高越接近近期低位。
                </p>
              </>
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
