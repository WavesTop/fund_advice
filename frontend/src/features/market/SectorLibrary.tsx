import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { EChartsOption } from 'echarts';
import { ListChecks, Plus, Search, X } from 'lucide-react';
import { Badge, EmptyState, Modal, Panel, searchInputProps } from '../../shared/ui';
import { Chart } from '../../shared/Chart';
import { sourceLabel } from '../../shared/source-label';
import { MarketDataRefresh } from './MarketDataRefresh';
import { parseSectorEvaluation } from './sector-evaluation-model';
import { readSectorCache, sectorCacheRevision, writeSectorCache } from './sector-request-state';
import { subjectKey } from './real-market-support';
import {
  calculateTechnical,
  detectTechnicalStages,
  technicalStageLabels,
  technicalStageSentiments,
  type PriceRow,
  type TechnicalPoint,
  type TechnicalStage,
} from './sector-technical';
import { type OpportunitiesResponse, type SectorOpportunity } from './SectorOpportunities';
import './sector-opportunities.css';
import './fund-directory.css';

type History = {
  code: string;
  source_id: string;
  universe_type: string;
  as_of: string | null;
  updated_at?: string | null;
  history_source_id: string;
  history_source_code: string;
  collection_error: string | null;
  rows: PriceRow[];
};

type ScanResult = {
  asOf: string | null;
  queriedAt?: string | null;
  stages: TechnicalStage[];
  points: TechnicalPoint[];
  error?: string;
};

type SectorSearchItem = {
  code: string;
  name: string;
  kind: '行业' | '概念' | '指数' | '参考指数' | 'ETF';
  source_id: string;
  universe_type?: 'hot_board' | 'tracked_index';
  index_name?: string;
};

type CollectionProgress = {
  stage?: string;
  state?: string;
  percent?: number;
  message?: string;
  current?: number;
  total?: number;
  dataset?: string;
  business_date?: string;
};

type CollectionStatus = {
  state: string;
  run_id: string | null;
  result?: {
    progress?: CollectionProgress;
    last_progress?: CollectionProgress;
    message?: string;
  } | null;
};

type RefreshProgress = {
  state: string;
  percent: number;
  title: string;
  message: string;
};

type TechnicalIndicator = 'macd' | 'kdj' | 'rsi' | 'wr';
type TechnicalSelections = Record<TechnicalIndicator, TechnicalStage[]>;
type SavedTechnicalCombination = {
  id: string;
  name: string;
  selections: TechnicalSelections;
  updatedAt: string;
};

const technicalCombinationStorageKey = 'sector-technical-combinations-v1';

const emptySelections = (): TechnicalSelections => ({
  macd: [],
  kdj: [],
  rsi: [],
  wr: [],
});

const defaultCombination = (): SavedTechnicalCombination => ({
  id: 'default-combination',
  name: '自定义组合 1',
  selections: emptySelections(),
  updatedAt: new Date().toISOString(),
});

const indicatorGroups: Array<{
  key: TechnicalIndicator;
  title: string;
  description: string;
  stages: TechnicalStage[];
}> = [
  {
    key: 'macd',
    title: 'MACD',
    description: 'DIF快线、DEA慢线与柱状线',
    stages: [
      'underwater-golden-cross',
      'above-zero-golden-cross',
      'white-near-zero',
      'yellow-near-zero',
      'above-zero-death-cross',
      'underwater-death-cross',
    ],
  },
  {
    key: 'kdj',
    title: 'KDJ',
    description: 'K快速线、D慢速线与J敏感方向线',
    stages: ['kdj-low-golden-cross', 'kdj-high-death-cross'],
  },
  {
    key: 'rsi',
    title: 'RSI',
    description: 'RSI6、RSI12、RSI24独立周期',
    stages: [
      'rsi6-low-rebound',
      'rsi6-high-fall',
      'rsi12-low-rebound',
      'rsi12-high-fall',
      'rsi24-low-rebound',
      'rsi24-high-fall',
    ],
  },
  {
    key: 'wr',
    title: 'WR',
    description: 'WR6短周期与WR10中周期',
    stages: ['wr6-high-risk', 'wr6-low-repair', 'wr10-high-risk', 'wr10-low-repair'],
  },
];

function sanitizeCombination(raw: unknown): SavedTechnicalCombination | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Partial<SavedTechnicalCombination>;
  if (typeof value.id !== 'string' || typeof value.name !== 'string') return null;
  const stageSet = new Set(indicatorGroups.flatMap((group) => group.stages));
  const selections = emptySelections();
  indicatorGroups.forEach((group) => {
    const stages = value.selections?.[group.key];
    selections[group.key] = Array.isArray(stages)
      ? stages.filter((stage): stage is TechnicalStage => stageSet.has(stage as TechnicalStage))
      : [];
  });
  return {
    id: value.id,
    name: value.name.trim() || '自定义组合',
    selections,
    updatedAt: typeof value.updatedAt === 'string' ? value.updatedAt : new Date().toISOString(),
  };
}

function readTechnicalCombinations(): SavedTechnicalCombination[] {
  try {
    const stored = window.localStorage.getItem(technicalCombinationStorageKey);
    if (!stored) return [defaultCombination()];
    const parsed = JSON.parse(stored);
    if (!Array.isArray(parsed)) return [defaultCombination()];
    const combinations = parsed
      .map(sanitizeCombination)
      .filter((item): item is SavedTechnicalCombination => !!item);
    return combinations.length ? combinations : [defaultCombination()];
  } catch {
    return [defaultCombination()];
  }
}

function writeTechnicalCombinations(combinations: SavedTechnicalCombination[]) {
  try {
    window.localStorage.setItem(technicalCombinationStorageKey, JSON.stringify(combinations));
  } catch {
    // Local storage is a convenience cache; filtering still works in memory.
  }
}

function formatDate(value: string | null | undefined) {
  return value?.slice(0, 10) || '暂无';
}

function quoteLabel(item: SectorOpportunity, result?: ScanResult) {
  const queriedAt = result ? result.queriedAt : item.updated_at;
  if (!queriedAt) return '行情查询 时间未知';
  const date = new Date(queriedAt);
  if (!Number.isFinite(date.getTime())) return '行情查询 时间未知';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const timestamp = `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
  return `行情查询 ${timestamp}`;
}

function formatPercent(value: number | null | undefined) {
  return value == null || !Number.isFinite(value)
    ? '—'
    : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function numberText(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits);
}

function latest(point: TechnicalPoint[] | undefined) {
  return point?.at(-1) ?? null;
}

function latestTradingChange(points: TechnicalPoint[] | undefined) {
  if (!points || points.length < 2)
    return { date: null as string | null, change: null as number | null };
  const current = points.at(-1);
  const previous = points.at(-2);
  const currentClose = Number(current?.close);
  const previousClose = Number(previous?.close);
  if (
    !current?.date ||
    !Number.isFinite(currentClose) ||
    !Number.isFinite(previousClose) ||
    previousClose === 0
  ) {
    return { date: current?.date ?? null, change: null };
  }
  return { date: current.date, change: ((currentClose - previousClose) / previousClose) * 100 };
}

function combinationSelectionCount(combination: SavedTechnicalCombination) {
  return Object.values(combination.selections).reduce((sum, stages) => sum + stages.length, 0);
}

function matchesCombination(
  result: ScanResult | undefined,
  combination: SavedTechnicalCombination,
) {
  const selectedGroups = indicatorGroups.filter(
    (group) => combination.selections[group.key].length > 0,
  );
  if (!selectedGroups.length) return true;
  if (selectedGroups.length) {
    if (!result || result.error || result.points.length < 35) return false;
    const stageSet = new Set(result.stages);
    const groupMatched = selectedGroups.every((group) =>
      combination.selections[group.key].some((stage) => stageSet.has(stage)),
    );
    if (!groupMatched) return false;
  }
  return true;
}

function boardLookupKey(item: { source_id: string; code: string; universe_type?: string }) {
  return `${item.universe_type ?? 'hot_board'}:${item.source_id}:${item.code}`;
}

function stageName(stage: string | undefined) {
  if (stage === 'market') return '同花顺行情与成分';
  if (stage === 'financials') return '成分经营与估值';
  return '板块数据刷新';
}

function progressFromStatus(status: CollectionStatus | null): RefreshProgress | null {
  if (!status || status.state === 'not_started') return null;
  const progress = status.result?.progress ?? status.result?.last_progress;
  const terminal = status.state !== 'running';
  const stage = progress?.stage;
  const percent = terminal
    ? 100
    : typeof progress?.percent === 'number'
      ? progress.percent
      : stage === 'financials'
        ? 82
        : stage === 'market'
          ? 38
          : 5;
  const detail =
    progress?.message ||
    status.result?.message ||
    (terminal ? '刷新任务已结束。' : '正在访问公开数据源。');
  return {
    state: status.state,
    percent,
    title:
      terminal && (status.state === 'failed' || status.state === 'timeout')
        ? '刷新未完成'
        : terminal && status.state === 'partial'
          ? '部分完成'
          : terminal
            ? '刷新完成'
            : stageName(stage),
    message:
      progress?.current && progress?.total
        ? `${detail}（${progress.current}/${progress.total}）`
        : detail,
  };
}

function historyUrl(item: SectorOpportunity) {
  const universe = item.universe_type ?? 'hot_board';
  const params = new URLSearchParams({ source_id: item.source_id, universe_type: universe });
  return `/api/sectors/${encodeURIComponent(item.code)}/series?${params}`;
}

function klineOption(item: SectorOpportunity, result: ScanResult): EChartsOption {
  const rows = result.points;
  const dates = rows.map((row) => row.date);
  const plotBackground = '#2f3a45';
  const axisText = '#d6dee6';
  const gridLine = '#465564';
  const grid = [
    { left: 60, right: 28, top: 64, height: 210 },
    { left: 60, right: 28, top: 322, height: 110 },
    { left: 60, right: 28, top: 480, height: 115 },
    { left: 60, right: 28, top: 643, height: 100 },
    { left: 60, right: 28, top: 792, height: 90 },
  ].map((entry) => ({
    ...entry,
    show: true,
    backgroundColor: plotBackground,
    borderColor: gridLine,
    borderWidth: 1,
  }));
  const axisBase = (gridIndex: number) => ({
    type: 'category' as const,
    gridIndex,
    data: dates,
    boundaryGap: true,
    axisLabel: {
      show: gridIndex === 4,
      color: axisText,
      hideOverlap: true,
      formatter: (value: string) => value.slice(5),
    },
    axisTick: { show: false },
    axisLine: { lineStyle: { color: gridLine } },
  });
  return {
    animation: false,
    backgroundColor: plotBackground,
    color: ['#326a79', '#b18a24', '#7a5aa6', '#bf4752', '#26806b', '#4c78a8'],
    grid,
    legend: {
      top: 12,
      left: 60,
      right: 24,
      itemWidth: 10,
      itemHeight: 8,
      textStyle: { color: axisText, fontSize: 11 },
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
        bottom: 18,
        height: 24,
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
        nameTextStyle: { color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: gridLine, type: 'dashed', opacity: 0.55 } },
      },
      {
        type: 'value',
        scale: true,
        gridIndex: 1,
        name: 'MACD',
        nameTextStyle: { color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: gridLine, type: 'dashed', opacity: 0.55 } },
      },
      {
        type: 'value',
        min: -50,
        max: 150,
        gridIndex: 2,
        name: 'KDJ',
        nameTextStyle: { color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: gridLine, type: 'dashed', opacity: 0.55 } },
      },
      {
        type: 'value',
        min: 0,
        max: 100,
        gridIndex: 3,
        name: 'RSI',
        nameTextStyle: { color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: gridLine, type: 'dashed', opacity: 0.55 } },
      },
      {
        type: 'value',
        min: -10,
        max: 110,
        gridIndex: 4,
        name: 'WR',
        nameTextStyle: { color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: gridLine, type: 'dashed', opacity: 0.55 } },
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: rows.map((row) => [row.open, row.close, row.low, row.high]),
        itemStyle: {
          color: '#bf4752',
          color0: '#26806b',
          borderColor: '#bf4752',
          borderColor0: '#26806b',
        },
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
        name: 'DIF快线',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: rows.map((row) => row.dif),
        showSymbol: false,
        lineStyle: { width: 1.5, color: '#f7fbff' },
        markLine: {
          symbol: 'none',
          data: [{ yAxis: 0 }],
          lineStyle: { color: '#9aa8b5', type: 'dashed' },
          label: { show: false },
        },
      },
      {
        name: 'DEA慢线',
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
  };
}

function stageTone(stage: TechnicalStage): 'blue' | 'amber' | 'green' | 'neutral' | 'red' {
  const sentiment = technicalStageSentiments[stage];
  if (sentiment === 'good') return 'green';
  if (sentiment === 'bad') return 'red';
  return 'amber';
}

function StageBadges({
  stages,
  insufficient,
}: {
  stages: TechnicalStage[];
  insufficient?: boolean;
}) {
  if (insufficient) return <Badge tone="neutral">指标不足</Badge>;
  if (!stages.length) return <Badge tone="neutral">暂无命中</Badge>;
  return (
    <>
      {stages.map((stage) => (
        <Badge key={stage} tone={stageTone(stage)}>
          {technicalStageLabels[stage]}
        </Badge>
      ))}
    </>
  );
}

export function SectorLibrary() {
  const [data, setData] = useState<OpportunitiesResponse | null>(readSectorCache);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>(() =>
    readSectorCache() ? 'ready' : 'loading',
  );
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [combinations, setCombinations] =
    useState<SavedTechnicalCombination[]>(readTechnicalCombinations);
  const [activeCombinationId, setActiveCombinationId] = useState(
    () => readTechnicalCombinations()[0]?.id ?? defaultCombination().id,
  );
  const [selectedKey, setSelectedKey] = useState('');
  const [scan, setScan] = useState({ done: 0, total: 0, running: false });
  const [technical, setTechnical] = useState<Record<string, ScanResult>>({});
  const [addQuery, setAddQuery] = useState('');
  const [searchingBoard, setSearchingBoard] = useState(false);
  const [addingBoard, setAddingBoard] = useState(false);
  const [addError, setAddError] = useState('');
  const [searchResults, setSearchResults] = useState<SectorSearchItem[]>([]);
  const [selectedAddKeys, setSelectedAddKeys] = useState<Set<string>>(new Set());
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedRemoveKeys, setSelectedRemoveKeys] = useState<Set<string>>(new Set());
  const [removingBoards, setRemovingBoards] = useState(false);
  const [removeError, setRemoveError] = useState('');
  const [progressVisible, setProgressVisible] = useState(false);
  const [refreshProgress, setRefreshProgress] = useState<RefreshProgress | null>(null);
  const [refreshingLibrary, setRefreshingLibrary] = useState(false);
  const request = useRef<AbortController | null>(null);
  const dataRef = useRef(data);
  const progressActive = useRef(false);
  const activeCombination =
    combinations.find((combination) => combination.id === activeCombinationId) ??
    combinations[0] ??
    defaultCombination();
  const selectedFilterCount = activeCombination ? combinationSelectionCount(activeCombination) : 0;

  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  useEffect(() => {
    writeTechnicalCombinations(combinations);
    if (!combinations.some((combination) => combination.id === activeCombinationId)) {
      setActiveCombinationId(combinations[0]?.id ?? defaultCombination().id);
    }
  }, [activeCombinationId, combinations]);

  const load = useCallback(() => {
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
        setState(dataRef.current ? 'ready' : 'error');
      });
    return controller;
  }, []);

  useEffect(() => {
    const controller = load();
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!progressVisible) return undefined;
    let stopped = false;
    const poll = async () => {
      try {
        const response = await fetch('/api/sectors/collection-status');
        if (!response.ok) return;
        const status = (await response.json()) as CollectionStatus;
        if (stopped) return;
        const next = progressFromStatus(status);
        if (next && (status.state !== 'running' || progressActive.current))
          setRefreshProgress(next);
        if (status.state === 'running' && !progressActive.current) stopped = true;
        if (status.state !== 'running' && !addingBoard && !refreshingLibrary) stopped = true;
      } catch {
        // The POST response remains authoritative; keep the visible in-flight state.
      }
    };
    void poll();
    const timer = window.setInterval(() => {
      if (stopped) window.clearInterval(timer);
      else void poll();
    }, 1000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [addingBoard, progressVisible, refreshingLibrary]);

  const boards = useMemo(
    () =>
      (data?.items ?? [])
        .filter(
          (item) => item.universe_type === 'hot_board' || item.universe_type === 'tracked_index',
        )
        .sort(
          (a, b) => (a.heat_rank ?? 9999) - (b.heat_rank ?? 9999) || a.code.localeCompare(b.code),
        ),
    [data],
  );
  const existingBoardKeys = useMemo(() => new Set(boards.map(boardLookupKey)), [boards]);
  const boardKey = boards
    .map((item) => `${subjectKey(item)}:${item.updated_at}:${item.as_of}`)
    .join('|');

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
          if (history.code !== item.code || history.source_id !== item.source_id)
            throw new Error('日线身份不一致');
          const points = calculateTechnical(history.rows);
          const stages = detectTechnicalStages(points);
          if (!cancelled) {
            setTechnical((values) => ({
              ...values,
              [key]: {
                asOf: history.as_of,
                queriedAt: history.updated_at,
                stages,
                points,
                error: history.collection_error || undefined,
              },
            }));
          }
        } catch (cause) {
          if (!cancelled && !(cause instanceof DOMException && cause.name === 'AbortError')) {
            setTechnical((values) => ({
              ...values,
              [key]: {
                asOf: null,
                stages: [],
                points: [],
                error: cause instanceof Error ? cause.message : '日线加载失败',
              },
            }));
          }
        } finally {
          if (!cancelled)
            setScan((value) => ({ ...value, done: Math.min(value.done + 1, value.total) }));
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
    const result = technical[key];
    if (!matchesCombination(result, activeCombination)) return false;
    return (
      !normalized ||
      item.name.toLowerCase().includes(normalized) ||
      item.code.toLowerCase().includes(normalized)
    );
  });
  const selected =
    boards.find((item) => subjectKey(item) === selectedKey) ?? filtered[0] ?? boards[0];
  const selectedResult = selected ? technical[subjectKey(selected)] : undefined;
  const current = latest(selectedResult?.points);
  const selectedDayChange = latestTradingChange(selectedResult?.points);
  const matchedCount = filtered.length;
  const searchCustomBoard = async () => {
    const term = addQuery.trim();
    if (!term || searchingBoard || addingBoard) return;
    setSearchingBoard(true);
    setAddError('');
    try {
      const response = await fetch(`/api/sector-board-search?q=${encodeURIComponent(term)}`);
      const payload = await response.json().catch(() => null);
      if (!response.ok)
        throw new Error(payload?.error?.message || `搜索失败（HTTP ${response.status}）。`);
      const items = Array.isArray(payload?.items) ? (payload.items as SectorSearchItem[]) : [];
      const addableItems = items.filter((item) => !existingBoardKeys.has(boardLookupKey(item)));
      if (items.length === 1 && !addableItems.length) {
        const item = items[0];
        setSelectedKey(boardLookupKey(item));
        setQuery(item.code);
        setSearchResults([]);
        setSelectedAddKeys(new Set());
        setShowAddModal(false);
        setAddError(`${item.name} 已在板块库中，已为你定位到列表。`);
        return;
      }
      setSearchResults(items);
      setSelectedAddKeys(
        new Set(addableItems.length === 1 ? [boardLookupKey(addableItems[0])] : []),
      );
      setShowAddModal(true);
      if (!items.length) setAddError('没有找到匹配的板块、指数或 ETF。');
    } catch (cause) {
      setAddError(cause instanceof Error ? cause.message : '板块搜索失败。');
    } finally {
      setSearchingBoard(false);
    }
  };
  const addSelectedBoards = async () => {
    const selected = searchResults.filter((item) => selectedAddKeys.has(boardLookupKey(item)));
    if (!selected.length || addingBoard) return;
    setAddingBoard(true);
    progressActive.current = true;
    setProgressVisible(true);
    setRefreshProgress({
      state: 'running',
      percent: 1,
      title: '准备添加板块',
      message: '已提交添加请求，刷新会在后台继续执行。',
    });
    setAddError('');
    try {
      const response = await fetch('/api/sectors/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: selected }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok)
        throw new Error(payload?.error?.message || `添加失败（HTTP ${response.status}）。`);
      if (payload?.evaluation) {
        const evaluation = parseSectorEvaluation(payload.evaluation);
        writeSectorCache(evaluation);
        setData(evaluation);
        setState('ready');
      } else {
        load();
      }
      setShowAddModal(false);
      setAddQuery('');
      setSearchResults([]);
      setSelectedAddKeys(new Set());
      const refresh = payload?.refresh;
      const refreshStatus = String(refresh?.status || 'success');
      setRefreshProgress({
        state: refreshStatus,
        percent: 100,
        title:
          refreshStatus === 'success'
            ? '刷新完成'
            : refreshStatus === 'partial'
              ? '部分完成'
              : '刷新未完成',
        message: refresh?.message || '板块已加入列表，日线和指标图已按现有流程更新。',
      });
    } catch (cause) {
      setAddError(cause instanceof Error ? cause.message : '板块添加失败。');
      setRefreshProgress({
        state: 'failed',
        percent: 100,
        title: '刷新未完成',
        message: cause instanceof Error ? cause.message : '板块添加失败。',
      });
    } finally {
      progressActive.current = false;
      setAddingBoard(false);
    }
  };
  const openEditModal = () => {
    setSelectedRemoveKeys(new Set());
    setRemoveError('');
    setShowEditModal(true);
  };
  const removeSelectedBoards = async () => {
    const selected = boards.filter((item) => selectedRemoveKeys.has(boardLookupKey(item)));
    if (!selected.length || removingBoards) return;
    setRemovingBoards(true);
    setRemoveError('');
    try {
      const response = await fetch('/api/sectors/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: selected.map((item) => ({
            universe_type: item.universe_type ?? 'hot_board',
            source_id: item.source_id,
            code: item.code,
            name: item.name,
          })),
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok)
        throw new Error(payload?.error?.message || `删除失败（HTTP ${response.status}）。`);
      if (payload?.evaluation) {
        const evaluation = parseSectorEvaluation(payload.evaluation);
        writeSectorCache(evaluation);
        setData(evaluation);
        setState('ready');
        setError('');
      } else {
        load();
      }
      setShowEditModal(false);
      setSelectedRemoveKeys(new Set());
    } catch (cause) {
      setRemoveError(cause instanceof Error ? cause.message : '删除板块失败。');
    } finally {
      setRemovingBoards(false);
    }
  };
  const updateActiveCombination = (
    updater: (combination: SavedTechnicalCombination) => SavedTechnicalCombination,
  ) => {
    setCombinations((items) =>
      items.map((item) => (item.id === activeCombination.id ? updater(item) : item)),
    );
  };
  const toggleStageFilter = (group: TechnicalIndicator, stage: TechnicalStage) => {
    updateActiveCombination((combination) => {
      const selected = new Set(combination.selections[group]);
      if (selected.has(stage)) selected.delete(stage);
      else selected.add(stage);
      return {
        ...combination,
        selections: {
          ...combination.selections,
          [group]: Array.from(selected),
        },
        updatedAt: new Date().toISOString(),
      };
    });
  };
  const createCombination = () => {
    const next: SavedTechnicalCombination = {
      id: `combination-${Date.now()}`,
      name: `自定义组合 ${combinations.length + 1}`,
      selections: emptySelections(),
      updatedAt: new Date().toISOString(),
    };
    setCombinations((items) => [...items, next]);
    setActiveCombinationId(next.id);
  };
  const renameCombination = () => {
    const name = window.prompt('重命名筛选目标', activeCombination.name)?.trim();
    if (!name) return;
    updateActiveCombination((combination) => ({
      ...combination,
      name,
      updatedAt: new Date().toISOString(),
    }));
  };
  const clearCombination = () => {
    updateActiveCombination((combination) => ({
      ...combination,
      selections: emptySelections(),
      updatedAt: new Date().toISOString(),
    }));
  };
  const deleteCombination = () => {
    if (combinations.length <= 1) return;
    const next = combinations.filter((item) => item.id !== activeCombination.id);
    const fallback = next.length ? next : [defaultCombination()];
    setCombinations(fallback);
    setActiveCombinationId(fallback[0].id);
  };

  return (
    <div className="sector-library">
      <div className="sector-toolbar">
        <p>
          读取本地行业板块日线。
          {data && (
            <small className="sector-method-version">
              评估于{' '}
              {new Date(data.generated_at).toLocaleString('zh-CN', {
                timeZone: 'Asia/Shanghai',
                hour12: false,
              })}{' '}
              · {data.method_version}
            </small>
          )}
        </p>
        <MarketDataRefresh
          target="sectors"
          idleLabel="刷新板块数据"
          busyLabel="正在刷新板块数据…"
          onStart={() => {
            request.current?.abort();
            progressActive.current = true;
            setRefreshingLibrary(true);
            setProgressVisible(true);
            setRefreshProgress({
              state: 'running',
              percent: 1,
              title: '准备刷新板块数据',
              message: '已提交刷新请求，正在等待后台开始采集。',
            });
          }}
          onSettled={(evaluation, outcome) => {
            progressActive.current = false;
            setRefreshingLibrary(false);
            if (outcome) {
              setProgressVisible(true);
              setRefreshProgress({
                state: outcome.status,
                percent: 100,
                title:
                  outcome.status === 'success'
                    ? '刷新完成'
                    : outcome.status === 'partial'
                      ? '部分完成'
                      : '刷新未完成',
                message: outcome.message,
              });
            }
            if (!evaluation) {
              load();
              return;
            }
            request.current?.abort();
            writeSectorCache(evaluation);
            setData(evaluation);
            setState('ready');
            setError('');
          }}
        />
      </div>
      <form
        className="sector-add-row"
        onSubmit={(event) => {
          event.preventDefault();
          void searchCustomBoard();
        }}
      >
        <label>
          添加板块
          <input
            {...searchInputProps}
            aria-label="输入同花顺板块名称或代码"
            placeholder="输入同花顺板块、指数或 ETF 名称/代码"
            value={addQuery}
            onChange={(event) => setAddQuery(event.target.value)}
          />
        </label>
        <button
          className="button secondary"
          type="submit"
          disabled={!addQuery.trim() || searchingBoard || addingBoard}
        >
          <Search size={16} aria-hidden="true" />
          {searchingBoard ? '搜索中…' : '搜索'}
        </button>
        {addError && !showAddModal && (
          <p role="alert" className="market-error">
            {addError}
          </p>
        )}
      </form>
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
        <label className="sector-combination-picker">
          筛选目标
          <select
            aria-label="选择自定义指标组合"
            value={activeCombination.id}
            onChange={(event) => setActiveCombinationId(event.target.value)}
          >
            {combinations.map((combination) => (
              <option key={combination.id} value={combination.id}>
                {combination.name}
              </option>
            ))}
          </select>
        </label>
        <div className="sector-combination-actions">
          <button className="button secondary small" type="button" onClick={createCombination}>
            新建
          </button>
          <button className="button secondary small" type="button" onClick={renameCombination}>
            重命名
          </button>
          <button
            className="button secondary small"
            type="button"
            disabled={!selectedFilterCount}
            onClick={clearCombination}
          >
            清空
          </button>
          <button
            className="button secondary small"
            type="button"
            disabled={combinations.length <= 1}
            onClick={deleteCombination}
          >
            删除
          </button>
        </div>
        <div className="sector-library-count">
          <strong>{boards.length}</strong> 个板块/指数 · 已扫描 {scan.done}/{scan.total}
          {scan.running ? ' · 扫描中' : ''}
          {(normalized || selectedFilterCount) && <> · 当前显示 {matchedCount}</>}
        </div>
      </div>
      <section className="sector-filter-builder" aria-label="技术阶段筛选">
        <div className="sector-filter-builder-head">
          <strong>{activeCombination.name}</strong>
          <span>{selectedFilterCount ? `已选 ${selectedFilterCount} 项` : '未选择指标条件'}</span>
        </div>
        <div className="sector-filter-groups">
          {indicatorGroups.map((group) => (
            <details key={group.key} className="sector-filter-menu">
              <summary>
                <span>
                  <strong>{group.title}</strong>
                  <small>{group.description}</small>
                </span>
                <b>{activeCombination.selections[group.key].length || '全部'}</b>
              </summary>
              <div className="sector-filter-menu-options">
                {group.stages.map((stageOption) => (
                  <label key={stageOption}>
                    <input
                      type="checkbox"
                      checked={activeCombination.selections[group.key].includes(stageOption)}
                      onChange={() => toggleStageFilter(group.key, stageOption)}
                    />
                    <span>{technicalStageLabels[stageOption]}</span>
                  </label>
                ))}
              </div>
            </details>
          ))}
        </div>
      </section>
      {error && (
        <p role="alert" className="market-error">
          {error}
        </p>
      )}
      {state === 'loading' && (
        <p role="status" className="sector-state">
          正在读取板块库…
        </p>
      )}
      {state === 'error' && (
        <EmptyState
          title="板块库暂时无法加载"
          description={error || '请确认本地数据服务已启动。'}
        />
      )}
      {state === 'ready' && !boards.length && (
        <EmptyState title="暂无板块库资料" description="本地行业板块池尚未采集成功。" />
      )}
      {state === 'ready' && !!boards.length && (
        <div className="sector-library-layout">
          <Panel
            className="sector-library-list"
            title="板块列表"
            subtitle="按真实行业池展示，点击查看右侧指标图。"
            action={
              <div className="sector-library-header-action">
                <div className="sector-tone-legend" aria-label="指标颜色含义">
                  <span>
                    <i className="sector-tone-dot green" />
                    绿=改善
                  </span>
                  <span>
                    <i className="sector-tone-dot red" />
                    红=风险
                  </span>
                  <span>
                    <i className="sector-tone-dot amber" />
                    黄=观察
                  </span>
                  <span>
                    <i className="sector-tone-dot neutral" />
                    灰=无信号/不足
                  </span>
                </div>
                <button className="button secondary small" type="button" onClick={openEditModal}>
                  <ListChecks size={15} aria-hidden="true" />
                  编辑列表
                </button>
              </div>
            }
          >
            <div className="sector-library-card-list">
              {filtered.map((item) => {
                const key = subjectKey(item);
                const result = technical[key];
                const dayChange = latestTradingChange(result?.points);
                return (
                  <button
                    key={key}
                    type="button"
                    className={`sector-library-card ${
                      selected && key === subjectKey(selected) ? 'sector-library-selected' : ''
                    }`}
                    onClick={() => setSelectedKey(key)}
                  >
                    <span className="sector-library-card-main">
                      <strong>{item.name}</strong>
                      <b
                        className={
                          dayChange.change == null
                            ? ''
                            : dayChange.change >= 0
                              ? 'sector-library-change-up'
                              : 'sector-library-change-down'
                        }
                      >
                        {formatPercent(dayChange.change)}
                      </b>
                    </span>
                    <span className="sector-library-card-meta">
                      <span>{item.code}</span>
                      <span>热度 #{item.heat_rank ?? '—'}</span>
                      <span>{quoteLabel(item, result)}</span>
                      <span>{sourceLabel(item.history_source_id ?? item.source_id)}</span>
                    </span>
                    <span className="sector-library-stages">
                      {result ? (
                        result.error ? (
                          <span className="muted">{result.error}</span>
                        ) : (
                          <StageBadges
                            stages={result.stages}
                            insufficient={result.points.length < 35}
                          />
                        )
                      ) : (
                        <span className="muted">待扫描</span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
            {!filtered.length && (
              <p className="research-empty">
                没有匹配的板块。扫描未完成时，自定义指标组合可能暂时为空。
              </p>
            )}
          </Panel>
          <Panel
            className="sector-library-chart"
            title={selected ? `${selected.name} · ${selected.code}` : '指标图'}
            subtitle={
              selected
                ? `${sourceLabel(selected.history_source_id ?? selected.source_id)} · ${quoteLabel(selected, selectedResult)}`
                : '请选择一个板块'
            }
          >
            {selected && (
              <div className="sector-library-summary">
                <span>
                  最近交易日 {formatDate(selectedDayChange.date)}
                  <strong>{formatPercent(selectedDayChange.change)}</strong>
                </span>
                <span>收盘 {numberText(current?.close, 2)}</span>
                <span>DIF快线 {numberText(current?.dif, 3)}</span>
                <span>DEA慢线 {numberText(current?.dea, 3)}</span>
                <span>MACD柱 {numberText(current?.macd, 3)}</span>
              </div>
            )}
            {!selected ? (
              <EmptyState title="请选择板块" description="左侧选择一个板块后显示 K 线和指标图。" />
            ) : !selectedResult ? (
              <p role="status" className="sector-state">
                正在读取该板块日线并计算指标…
              </p>
            ) : selectedResult.points.length < 35 ? (
              <EmptyState
                title="指标图暂不可用"
                description={
                  selectedResult.error || '日线样本不足，无法计算 MACD、KDJ、RSI 与 WR。'
                }
              />
            ) : (
              <>
                <div className="sector-library-stages sector-library-current">
                  <StageBadges
                    stages={selectedResult.stages}
                    insufficient={selectedResult.points.length < 35}
                  />
                </div>
                <Chart
                  option={klineOption(selected, selectedResult)}
                  label={`${selected.name} K线以及 MACD、KDJ、RSI、WR 四类常用指标图`}
                  height={960}
                />
                <p className="muted">
                  MACD：DIF为快线，DEA为慢线/信号线，MACD柱反映DIF与DEA差值；KDJ：K为快速线，D为慢速线，J对短期拐点更敏感；RSI6/12/24分别反映短、中、长周期相对强弱；WR数值越低越接近近期高位，越高越接近近期低位。
                </p>
              </>
            )}
          </Panel>
        </div>
      )}
      {showAddModal && (
        <Modal title="添加板块/指数/ETF" onClose={() => setShowAddModal(false)}>
          <div className="sector-add-modal">
            {searchResults.length ? (
              <>
                <p className="muted">
                  选择后会加入本地板块库，并立即按现有刷新流程获取日线和指标图。
                </p>
                <div className="sector-add-results">
                  {searchResults.map((item) => {
                    const key = boardLookupKey(item);
                    const alreadyExists = existingBoardKeys.has(key);
                    return (
                      <label key={key}>
                        <input
                          type="checkbox"
                          disabled={alreadyExists}
                          checked={selectedAddKeys.has(key)}
                          onChange={(event) => {
                            const next = new Set(selectedAddKeys);
                            if (event.target.checked) next.add(key);
                            else next.delete(key);
                            setSelectedAddKeys(next);
                          }}
                        />
                        <span>
                          <strong>{item.name}</strong>
                          <small>
                            {item.code} · {item.kind} · {sourceLabel(item.source_id)}
                          </small>
                          {item.index_name && item.index_name !== item.name && (
                            <small>{item.index_name}</small>
                          )}
                          {alreadyExists && <Badge tone="neutral">已在板块库</Badge>}
                        </span>
                      </label>
                    );
                  })}
                </div>
                <div className="modal-actions">
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => setShowAddModal(false)}
                  >
                    取消
                  </button>
                  <button
                    className="button primary"
                    type="button"
                    disabled={!selectedAddKeys.size || addingBoard}
                    onClick={() => void addSelectedBoards()}
                  >
                    <Plus size={16} aria-hidden="true" />
                    {addingBoard
                      ? '添加并刷新中…'
                      : selectedAddKeys.size === searchResults.length
                        ? '全部添加'
                        : '添加选中'}
                  </button>
                </div>
                {addingBoard && (
                  <p role="status" className="sector-add-progress-inline">
                    正在后台刷新，关闭这个窗口后任务仍会继续。
                  </p>
                )}
              </>
            ) : (
              <EmptyState
                title="没有找到匹配板块"
                description="请换一个板块或指数名称、六位代码再搜索。"
              />
            )}
            {addError && (
              <p role="alert" className="market-error">
                {addError}
              </p>
            )}
          </div>
        </Modal>
      )}
      {showEditModal && (
        <Modal title="编辑板块列表" onClose={() => setShowEditModal(false)} wide>
          <div className="sector-edit-modal">
            <p className="muted">
              勾选后确认删除，会从本地板块库中移除；后续刷新也会按删除后的列表计算数量。
            </p>
            <div className="sector-edit-results">
              {boards.map((item) => {
                const key = boardLookupKey(item);
                return (
                  <label key={key}>
                    <input
                      type="checkbox"
                      checked={selectedRemoveKeys.has(key)}
                      onChange={(event) => {
                        const next = new Set(selectedRemoveKeys);
                        if (event.target.checked) next.add(key);
                        else next.delete(key);
                        setSelectedRemoveKeys(next);
                      }}
                    />
                    <span>
                      <strong>{item.name}</strong>
                      <small>
                        {item.code} ·{' '}
                        {item.universe_type === 'tracked_index' ? '参考指数' : item.kind} ·{' '}
                        {sourceLabel(item.source_id)}
                      </small>
                    </span>
                  </label>
                );
              })}
            </div>
            <div className="modal-actions">
              <button
                className="button secondary"
                type="button"
                onClick={() => setShowEditModal(false)}
              >
                取消
              </button>
              <button
                className="button danger"
                type="button"
                disabled={!selectedRemoveKeys.size || removingBoards}
                onClick={() => void removeSelectedBoards()}
              >
                {removingBoards ? '删除中…' : `删除选中 ${selectedRemoveKeys.size} 个`}
              </button>
            </div>
            {removeError && (
              <p role="alert" className="market-error">
                {removeError}
              </p>
            )}
          </div>
        </Modal>
      )}
      {progressVisible && refreshProgress && (
        <aside className="sector-refresh-toast" role="status" aria-label="板块刷新进度">
          <button
            type="button"
            className="icon-button"
            aria-label="关闭刷新进度"
            onClick={() => setProgressVisible(false)}
          >
            <X size={16} />
          </button>
          <strong>{refreshProgress.title}</strong>
          <span>{Math.round(refreshProgress.percent)}%</span>
          <div className="sector-refresh-bar" aria-hidden="true">
            <i style={{ width: `${Math.max(0, Math.min(100, refreshProgress.percent))}%` }} />
          </div>
          <p>{refreshProgress.message}</p>
        </aside>
      )}
    </div>
  );
}
