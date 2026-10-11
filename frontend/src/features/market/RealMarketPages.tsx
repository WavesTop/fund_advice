import { useEffect, useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { ChartNoAxesCombined, ChevronLeft, Database } from 'lucide-react';
import type { EChartsOption } from 'echarts';
import { EmptyState, PageHeader, Panel, Tabs } from '../../shared/ui';
import { sourceLabel } from '../../shared/source-label';
import { Chart } from '../../shared/Chart';
import { recordFundVisit } from './fund-browse-history';
import { FundDirectory as RealFundDirectory } from './FundDirectory';
import { RealSectorDetail } from './RealSectorDetail';
import { SectorLibrary } from './SectorLibrary';
import { SectorOpportunities } from './SectorOpportunities';
import { researchHref, safeReturnPath, sourceHref } from './real-market-support';
import { alignRows, commonDates, rangeDates } from './series-alignment';
import './market.css';

function MarketNavigation({ selected }: { selected: 'funds' | 'sectors' | 'library' }) {
  return (
    <nav className="market-subnav" aria-label="基金与板块导航">
      <Link className={selected === 'funds' ? 'active' : ''} to="/funds">
        <Database size={16} />
        基金库
      </Link>
      <Link className={selected === 'library' ? 'active' : ''} to="/sectors?tab=library">
        <Database size={16} />
        板块库
      </Link>
      <Link className={selected === 'sectors' ? 'active' : ''} to="/sectors">
        <ChartNoAxesCombined size={16} />
        板块机会
      </Link>
    </nav>
  );
}
type RealFund = {
  share_id: string;
  code: string;
  name: string;
  fund_type: string;
  source_id: string;
};

type RealPriceRow = {
  date: string;
  open: string | null;
  high: string | null;
  low: string | null;
  close: string | null;
  volume: string | null;
  amount: string | null;
};

type RealNavRow = {
  date: string;
  unit_nav: string | null;
  accumulated_nav?: string | null;
  cumulative_nav?: string | null;
};

type RealFundSeriesResponse = {
  kind: 'price' | 'nav' | null;
  rows: RealPriceRow[] | RealNavRow[];
  source_id: string | null;
  policy_version: string | null;
  updated_at: string | null;
};

type RealFundDetailResponse = {
  fund: RealFund;
  series: RealFundSeriesResponse | null;
  related_market?: RelatedMarket | null;
  related_markets?: RelatedMarket[];
  series_options?: RealFundSeriesResponse[];
  refresh?: {
    status: 'success' | 'partial' | 'failed';
    stages: Record<string, { status: string; message: string }>;
  };
};

function refreshStageSummary(
  stages: Record<string, { status: string; message: string }> | null | undefined,
) {
  if (!stages) return '';
  return Object.entries(stages)
    .filter(([, stage]) => !['updated', 'preserved'].includes(stage.status))
    .map(
      ([name, stage]) =>
        `${name === 'fund_series' ? '基金序列' : name === 'related_market' ? '关联指数' : name}：${stage.message}`,
    )
    .join('；');
}

type RelatedMarket = {
  name: string;
  code: string;
  kind: 'index';
  source_id: string;
  relation_status?: 'linked' | 'withheld' | 'superseded';
  relation_source_id?: string;
  verified_at?: string;
  evidence_url?: string;
  relation_reason?: string;
  market_status?: 'ready' | 'failed' | 'stale' | 'missing' | 'invalid';
  market_reason?: string;
  updated_at?: string;
  rows: RealPriceRow[];
};
function RealFundSeries({
  series,
  code,
  refreshing,
  refreshError,
  onRefresh,
  dateAxis,
}: {
  series: RealFundSeriesResponse | null;
  code: string;
  refreshing: boolean;
  refreshError: string;
  onRefresh: () => void;
  dateAxis: string[];
}) {
  const refreshButton = (
    <button className="button secondary" type="button" onClick={onRefresh} disabled={refreshing}>
      {refreshing ? '正在采集…' : series?.kind && series.rows.length ? '更新数据' : '获取真实数据'}
    </button>
  );
  if (!series || !series.kind || !series.rows.length)
    return (
      <Panel title="真实行情序列" action={refreshButton}>
        <div className="real-catalog-state">
          <p>该基金尚未采集可展示的净值或 K 线序列。</p>
          {refreshError && (
            <p className="notice notice-error" role="alert">
              {refreshError}
            </p>
          )}
        </div>
      </Panel>
    );

  const dates = dateAxis;
  const numberValue = (value: string | null) => (value == null ? null : Number(value));
  const isPrice = series.kind === 'price';
  const priceRows = isPrice ? alignRows(series.rows as RealPriceRow[], dates) : [];
  const navRows = !isPrice ? alignRows(series.rows as RealNavRow[], dates) : [];
  const option: EChartsOption = {
    animation: false,
    legend: { top: 0 },
    grid: isPrice
      ? [
          { left: 58, right: 20, top: 24, height: 220 },
          { left: 58, right: 20, top: 275, height: 60 },
        ]
      : { left: 58, right: 20, top: 24, bottom: 38 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    xAxis: isPrice
      ? [
          { type: 'category', data: dates, boundaryGap: true, axisLabel: { show: false } },
          {
            type: 'category',
            data: dates,
            gridIndex: 1,
            axisLabel: {
              color: '#5d6c80',
              hideOverlap: true,
              formatter: (value: string) => value.slice(5),
            },
          },
        ]
      : {
          type: 'category',
          data: dates,
          axisLabel: {
            color: '#5d6c80',
            hideOverlap: true,
            formatter: (value: string) => value.slice(5),
          },
        },
    yAxis: isPrice
      ? [
          {
            type: 'value',
            scale: true,
            name: '价格',
            axisLabel: { color: '#5d6c80' },
            splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
          },
          {
            type: 'value',
            gridIndex: 1,
            name: '成交量',
            axisLabel: { color: '#5d6c80' },
            splitLine: { show: false },
          },
        ]
      : {
          type: 'value',
          scale: true,
          name: '净值',
          axisLabel: { color: '#5d6c80' },
          splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
        },
    dataZoom: [
      { type: 'inside', xAxisIndex: isPrice ? [0, 1] : [0], start: 0, end: 100 },
      {
        type: 'slider',
        xAxisIndex: isPrice ? [0, 1] : [0],
        bottom: 5,
        height: 20,
        showDetail: false,
      },
    ],
    series: isPrice
      ? [
          {
            name: '真实日 K',
            type: 'candlestick',
            data: priceRows.map((row) => [
              numberValue(row?.open ?? null),
              numberValue(row?.close ?? null),
              numberValue(row?.low ?? null),
              numberValue(row?.high ?? null),
            ]),
          },
          {
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: priceRows.map((row) => numberValue(row?.volume ?? null)),
          },
        ]
      : [
          {
            name: '单位净值',
            type: 'line',
            data: navRows.map((row) => numberValue(row?.unit_nav ?? null)),
            showSymbol: false,
            connectNulls: false,
            lineStyle: { width: 2 },
          },
          {
            name: '累计净值',
            type: 'line',
            data: navRows.map((row) =>
              numberValue(row?.accumulated_nav ?? row?.cumulative_nav ?? null),
            ),
            showSymbol: false,
            connectNulls: false,
            lineStyle: { width: 2, type: 'dashed' },
          },
        ],
  };
  return (
    <Panel
      title={isPrice ? '真实 K 线' : '真实净值走势'}
      action={refreshButton}
      subtitle={`行情截至 ${series.rows.at(-1)?.date} · ${sourceLabel(series.source_id, '来源待补充')}${series.updated_at ? ` · 入库于 ${series.updated_at.slice(0, 10)}` : ''}`}
    >
      {refreshError && (
        <div className="notice notice-error" role="alert">
          {refreshError}
        </div>
      )}
      <p className="muted">
        源数据区间 {series.rows[0].date} 至 {series.rows.at(-1)?.date} · {series.rows.length} 条记录
        · 口径 {series.policy_version || '待提供'}。价格或净值曲线不直接等于含分红、费用的投资收益。
      </p>
      <Chart
        option={option}
        label={`${code}${isPrice ? '真实K线' : '真实净值走势'}`}
        height={390}
        linkGroup={`fund-market-${code}`}
      />
    </Panel>
  );
}

function RelatedMarketPanel({
  market,
  dateAxis,
  fundCode,
}: {
  market: RelatedMarket | null | undefined;
  dateAxis: string[];
  fundCode: string;
}) {
  if (!market) return null;
  if (market.relation_status !== 'linked')
    return (
      <Panel title="关联待核验">
        <p>{market.relation_reason || '关联来源或核验状态不足，不绘制默认指数。'}</p>
      </Panel>
    );
  if (!market.rows.length)
    return (
      <Panel
        title="关联指数行情"
        subtitle={`${market.name} · ${market.code} · ${sourceLabel(market.source_id)}`}
      >
        <p>{market.market_reason || '关系已记录，但该指数尚无可用日线。'}</p>
        <p className="muted">基金—指数关系仍可核对；旧行情不会进入当前走势或强弱信号。</p>
      </Panel>
    );
  const numberValue = (value: string | null) => (value == null ? null : Number(value));
  const dates = dateAxis;
  const aligned = alignRows(market.rows, dates);
  const option: EChartsOption = {
    animation: false,
    legend: { top: 0 },
    grid: { left: 58, right: 20, top: 24, bottom: 38 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: true,
      axisLabel: {
        color: '#5d6c80',
        hideOverlap: true,
        formatter: (value: string) => value.slice(5),
      },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: '指数价格',
      axisLabel: { color: '#5d6c80' },
      splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 5, height: 20 },
    ],
    series: [
      {
        name: '真实指数 K 线',
        type: 'candlestick',
        data: aligned.map((row) => [
          numberValue(row?.open ?? null),
          numberValue(row?.close ?? null),
          numberValue(row?.low ?? null),
          numberValue(row?.high ?? null),
        ]),
      },
    ],
  };
  return (
    <Panel
      title="关联指数行情"
      subtitle={`${market.name} · ${market.code} · ${sourceLabel(market.source_id)}`}
      action={
        <Link
          className="button secondary"
          to={researchHref({
            code: market.code,
            source_id: market.source_id,
            universe_type: 'tracked_index',
          })}
        >
          查看三周期研究
        </Link>
      }
    >
      <p className="muted">
        行情截至 {market.rows.at(-1)?.date} · 关系核验于 {market.verified_at || '未提供'} · 关系来源{' '}
        {sourceLabel(market.relation_source_id)}{' '}
        {sourceHref(market.evidence_url) && (
          <a href={sourceHref(market.evidence_url)!} target="_blank" rel="noreferrer">
            核对证据
          </a>
        )}
      </p>
      <p className="muted">已存储的基金—指数关系不等于板块投资推荐；指数表现不等于基金实际回报。</p>
      <Chart
        option={option}
        label={`${market.code}关联板块真实K线`}
        height={340}
        linkGroup={`fund-market-${fundCode}`}
      />
    </Panel>
  );
}

export function FundsPage() {
  return (
    <div className="market-page">
      <PageHeader
        eyebrow="MARKET EXPLORER"
        title="基金库"
        description="查询已收录的真实基金身份信息。"
      />
      <MarketNavigation selected="funds" />
      <RealFundDirectory />
    </div>
  );
}

export function FundDetailPage() {
  const { code = '' } = useParams();
  // A route change creates a new state owner; an old refresh cannot replace a new fund.
  return <RealFundDetail key={code} />;
}

function RealFundDetail() {
  const { code = '' } = useParams();
  const [params] = useSearchParams();
  const [fund, setFund] = useState<RealFund | null>(null);
  const [series, setSeries] = useState<RealFundSeriesResponse | null>(null);
  const [relatedMarket, setRelatedMarket] = useState<RelatedMarket | null>(null);
  const [relatedMarkets, setRelatedMarkets] = useState<RelatedMarket[]>([]);
  const [seriesOptions, setSeriesOptions] = useState<RealFundSeriesResponse[]>([]);
  const [seriesKind, setSeriesKind] = useState<'price' | 'nav' | null>(null);
  const [chartRange, setChartRange] = useState('all');
  const [chartStart, setChartStart] = useState('');
  const [chartEnd, setChartEnd] = useState('');
  const selectedSeries = seriesOptions.find((option) => option.kind === seriesKind) ?? series;
  const dates = useMemo(
    () =>
      commonDates([
        selectedSeries?.rows ?? [],
        relatedMarket?.relation_status === 'linked' ? relatedMarket.rows : [],
      ]),
    [selectedSeries, relatedMarket],
  );
  const dateAxis = useMemo(
    () => rangeDates(dates, chartRange, chartStart, chartEnd),
    [dates, chartRange, chartStart, chartEnd],
  );
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'missing'>('loading');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/funds/${encodeURIComponent(code)}`, { signal: controller.signal })
      .then(async (response) => {
        if (response.status === 404) {
          setStatus('missing');
          return null;
        }
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then((data: RealFundDetailResponse | null) => {
        if (controller.signal.aborted) return;
        if (data?.fund) {
          if (
            data.fund.code !== code ||
            !/^[0-9]{6}$/.test(code) ||
            typeof data.fund.name !== 'string' ||
            !data.fund.name.trim()
          ) {
            throw new Error('基金身份与请求不一致');
          }
          recordFundVisit(code);
          setFund(data.fund);
          setSeries(data.series ?? null);
          setRelatedMarket(data.related_market ?? null);
          setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
          setRelatedMarkets(data.related_markets ?? []);
          setStatus('ready');
        } else setStatus('missing');
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setStatus('error');
      });
    return () => controller.abort();
  }, [code]);
  const refresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError('');
    fetch(`/api/funds/${encodeURIComponent(code)}/refresh`, { method: 'POST' })
      .then(async (response) => {
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          const current = data?.error?.details?.current as RealFundDetailResponse | undefined;
          if (current?.fund?.code === code) {
            setFund(current.fund);
            setSeries(current.series ?? null);
            setSeriesOptions(
              current.series_options ?? (current.series?.kind ? [current.series] : []),
            );
            setRelatedMarket(current.related_market ?? null);
            setRelatedMarkets(current.related_markets ?? []);
          }
          const stages = refreshStageSummary(data?.error?.details?.refresh?.stages);
          const message = data?.error?.message || '真实数据采集失败，请稍后重试。';
          throw new Error(stages ? `${message} ${stages}` : message);
        }
        if (!data?.fund || data.fund.code !== code)
          throw new Error('返回的基金身份与请求不一致，旧数据已保留。');
        return data as RealFundDetailResponse;
      })
      .then((data) => {
        setFund(data.fund);
        setSeries(data.series ?? null);
        setRelatedMarket(data.related_market ?? null);
        setRelatedMarkets(data.related_markets ?? []);
        setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
        if (data.refresh?.status === 'partial') {
          setRefreshError(`本次部分更新成功。${refreshStageSummary(data.refresh.stages)}`);
        }
      })
      .catch((cause: unknown) => {
        if (cause instanceof TypeError) {
          setRefreshError('本地数据服务未连接。请重新启动本地网页后再试。');
          return;
        }
        setRefreshError(cause instanceof Error ? cause.message : '真实数据采集失败，请稍后重试。');
      })
      .finally(() => setRefreshing(false));
  };
  const back = safeReturnPath(params.get('from'), '/funds');
  if (status === 'loading')
    return (
      <div className="market-page">
        <div className="real-catalog-state" role="status">
          正在读取真实基金身份…
        </div>
      </div>
    );
  if (status === 'error')
    return (
      <div className="market-page">
        <EmptyState
          title="真实基金身份暂时无法加载"
          description="请确认本地数据服务已启动后重试。"
          action={
            <Link className="button primary" to={back}>
              返回基金库
            </Link>
          }
        />
      </div>
    );
  if (status === 'missing' || !fund)
    return (
      <div className="market-page">
        <EmptyState
          title="未找到这只基金"
          description={`本地真实目录中没有代码 ${code} 的身份资料。`}
          action={
            <Link className="button primary" to={back}>
              返回基金库
            </Link>
          }
        />
      </div>
    );
  return (
    <div className="market-page">
      <Link className="market-back" to={back}>
        <ChevronLeft size={15} />
        {back.startsWith('/advice') ? '返回研究页' : '返回基金库'}
      </Link>
      <PageHeader
        eyebrow="FUND PROFILE"
        title={fund.name}
        description={`${fund.code} / ${fund.fund_type || '基金类型待补充'}`}
        actions={
          <Link
            className="button primary"
            to={`/advice?tab=research&fund=${fund.code}&from=${encodeURIComponent(`/funds/${fund.code}`)}`}
          >
            查看该基金的关联研究
          </Link>
        }
      />
      <Panel title="基金身份">
        <dl className="market-overview">
          <div>
            <dt>基金代码</dt>
            <dd>{fund.code}</dd>
          </div>
          <div>
            <dt>份额 ID</dt>
            <dd>{fund.share_id}</dd>
          </div>
          <div>
            <dt>基金类型</dt>
            <dd>{fund.fund_type || '—'}</dd>
          </div>
          <div>
            <dt>数据来源</dt>
            <dd>{sourceLabel(fund.source_id, '—')}</dd>
          </div>
        </dl>
      </Panel>
      {seriesOptions.length > 1 && (
        <Tabs
          value={selectedSeries?.kind ?? 'nav'}
          onChange={(value) => setSeriesKind(value as 'price' | 'nav')}
          items={seriesOptions.map((option) => ({
            value: option.kind!,
            label: option.kind === 'price' ? '交易价格' : '基金净值',
          }))}
        />
      )}
      {!!dates.length && (
        <Panel
          title="共同查看区间"
          subtitle="同步日期与缩放，不等于同口径收益对比；未接入交易日历，无法识别双方共同缺失的交易日。"
        >
          <Tabs
            value={chartRange}
            onChange={setChartRange}
            items={[
              { value: 'month', label: '近 1 月' },
              { value: 'quarter', label: '近 3 月' },
              { value: 'half', label: '近 6 月' },
              { value: 'year', label: '近 1 年' },
              { value: 'all', label: '全部' },
              { value: 'custom', label: '自定义' },
            ]}
          />
          {chartRange === 'custom' && (
            <div className="toolbar">
              <label>
                起始日期
                <input
                  aria-label="图表起始日期"
                  type="date"
                  min={dates[0]}
                  max={dates.at(-1)}
                  value={chartStart}
                  onChange={(event) => setChartStart(event.target.value)}
                />
              </label>
              <label>
                截止日期
                <input
                  aria-label="图表截止日期"
                  type="date"
                  min={dates[0]}
                  max={dates.at(-1)}
                  value={chartEnd}
                  onChange={(event) => setChartEnd(event.target.value)}
                />
              </label>
            </div>
          )}
          {!dateAxis.length && <p role="alert">请选择有效且有数据的日期区间。</p>}
        </Panel>
      )}
      <RealFundSeries
        code={fund.code}
        series={selectedSeries}
        refreshing={refreshing}
        refreshError={refreshError}
        onRefresh={refresh}
        dateAxis={dateAxis}
      />
      <RelatedMarketPanel market={relatedMarket} dateAxis={dateAxis} fundCode={fund.code} />
      {relatedMarkets.some((relation) => relation.relation_status !== 'linked') && (
        <Panel title="历史关联与核验缺项">
          {relatedMarkets
            .filter((relation) => relation.relation_status !== 'linked')
            .map((relation) => (
              <p key={`${relation.code}-${relation.relation_status}`}>
                {relation.name} · {relation.code}：
                {relation.relation_reason || '尚未通过当前核验。'}
              </p>
            ))}
        </Panel>
      )}
      {!relatedMarket && (
        <Panel title="对应指数 / 板块">
          <p className="muted">
            尚无已核验的对应关系，不按基金名称猜测行业，也不套用其他基金的板块资料。
          </p>
        </Panel>
      )}
      <Panel title="研究与基金推荐的资料边界">
        <p>
          {selectedSeries?.rows.length
            ? '基金身份和当前可用走势已展示；基金披露持仓、行业穿透、完整费用、跟踪表现及同类择优尚未接入此页。'
            : '当前仅有基金目录身份，尚无可展示的真实走势；基金披露持仓、行业穿透、完整费用、跟踪表现及同类择优也尚未接入此页。'}
        </p>
        <p className="muted">
          缺项不表示零持仓、零费用或不值得投资。关联研究只展示当前可核验内容，不生成买入金额。
        </p>
      </Panel>
    </div>
  );
}

export function SectorsPage() {
  const [params] = useSearchParams();
  const tab = params.get('tab') === 'library' ? 'library' : 'opportunities';
  return (
    <div className="market-page">
      <PageHeader
        eyebrow={tab === 'library' ? 'SECTOR LIBRARY' : 'SECTOR OPPORTUNITIES'}
        title={tab === 'library' ? '板块库' : '板块机会'}
        description={tab === 'library'
          ? '查看本地行业板块日线、MACD、KDJ、RSI、WR 指标图，并筛选常见技术阶段。'
          : '结合行业经营、估值线索与真实走势，观察三个周期的支持证据和待确认条件。'}
      />
      <MarketNavigation selected={tab === 'library' ? 'library' : 'sectors'} />
      {tab === 'library' ? <SectorLibrary /> : <SectorOpportunities />}
    </div>
  );
}

export function SectorDetailPage() {
  return <RealSectorDetail />;
}
