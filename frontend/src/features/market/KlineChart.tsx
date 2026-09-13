import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { Badge, EmptyState, Panel, Tabs } from '../../shared/ui';
import { DATA_DATE, funds, signed } from '../../shared/data';
import { makeDemoCandles, movingAverage } from './kline-model';
import './kline.css';

const averages = [
  { days: 5, color: '#88630b', line: 'solid' as const },
  { days: 10, color: '#7852a3', line: 'dashed' as const },
  { days: 20, color: '#326b9e', line: 'dotted' as const },
];
const rise = '#bf4752';
const fall = '#26806b';
const price = (value: number) => value.toFixed(4);

export function KlinePreview() {
  const [code, setCode] = useState('510300');
  return (
    <KlinePanel
      key={code}
      code={code}
      preview
      action={
        <label className="kline-fund-picker">
          演示基金
          <select
            aria-label="K线演示基金"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          >
            {funds
              .filter((fund) => fund.chart === 'price')
              .map((fund) => (
                <option key={fund.code} value={fund.code}>
                  {fund.name} · {fund.code}
                </option>
              ))}
          </select>
        </label>
      }
    />
  );
}

export function KlinePanel({
  code,
  preview = false,
  action,
}: {
  code: string;
  preview?: boolean;
  action?: ReactNode;
}) {
  const fund = funds.find((item) => item.code === code)!;
  const full = useMemo(() => makeDemoCandles(code), [code]);
  const [range, setRange] = useState('quarter');
  const [start, setStart] = useState('2026-08-11');
  const [end, setEnd] = useState(DATA_DATE);
  const [enabled, setEnabled] = useState([5, 10, 20]);
  const [resetKey, setResetKey] = useState(0);
  const cutoff =
    Date.parse(DATA_DATE) -
    ({ month: 31, quarter: 93, half: 186, year: 366, all: Infinity }[range] ?? 93) * 86_400_000;
  const invalid =
    range === 'custom' &&
    (!start || !end || start > end || start < full[0].date || end > DATA_DATE);
  const indexed = full
    .map((point, index) => ({ ...point, index }))
    .filter((point) =>
      range === 'custom'
        ? !invalid && point.date >= start && point.date <= end
        : Date.parse(point.date) >= cutoff,
    );
  const last = indexed.at(-1);
  const previous = last ? full[last.index - 1] : undefined;
  const change = last && previous ? (last.close / previous.close - 1) * 100 : undefined;
  const lines = averages
    .filter((item) => enabled.includes(item.days))
    .map((item) => {
      const values = movingAverage(full, item.days);
      return {
        name: `MA${item.days}`,
        type: 'line' as const,
        data: indexed.map((point) => values[point.index]),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 1.5, color: item.color, type: item.line },
        itemStyle: { color: item.color },
      };
    });
  const option: EChartsOption = {
    animation: false,
    grid: [
      { left: 56, right: 18, top: 25, height: 210 },
      { left: 56, right: 18, top: 272, height: 65 },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#263f58' } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      renderMode: 'richText',
      confine: true,
      textStyle: { color: '#263f58', fontSize: 12 },
      formatter: (params) => {
        const items = Array.isArray(params) ? params : [params];
        const index =
          items.find((item) => item.seriesType === 'candlestick')?.dataIndex ?? items[0]?.dataIndex;
        const point = index == null ? undefined : indexed[index];
        if (!point) return '';
        return `${point.date} · 模拟日线\n开盘  ${price(point.open)} 元\n收盘  ${price(point.close)} 元\n最高  ${price(point.high)} 元\n最低  ${price(point.low)} 元\n成交量  ${(point.volume / 10000).toFixed(2)} 万份`;
      },
    },
    xAxis: [0, 1].map((gridIndex) => ({
      type: 'category' as const,
      gridIndex,
      data: indexed.map((point) => point.date),
      boundaryGap: true,
      axisLine: { lineStyle: { color: '#dbe4ed' } },
      axisTick: { show: false },
      axisLabel: {
        show: gridIndex === 1,
        color: '#5d6c80',
        hideOverlap: true,
        formatter: (value: string) => value.slice(5),
      },
      axisPointer: { label: { show: true } },
    })),
    yAxis: [
      {
        type: 'value',
        scale: true,
        name: '价格 / 元',
        splitNumber: 4,
        nameTextStyle: { color: '#5d6c80' },
        axisLabel: { color: '#5d6c80', formatter: (value: number) => value.toFixed(2) },
        splitLine: { lineStyle: { color: '#e9eef4', type: 'dashed' } },
      },
      {
        type: 'value',
        gridIndex: 1,
        name: '成交量 / 万份',
        splitNumber: 2,
        nameTextStyle: { color: '#5d6c80' },
        axisLabel: { color: '#5d6c80', formatter: (value: number) => (value / 10000).toFixed(0) },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1],
        start: 0,
        end: 100,
        minValueSpan: 4,
        zoomOnMouseWheel: 'ctrl',
        moveOnMouseWheel: false,
      },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        bottom: 8,
        height: 22,
        left: 56,
        right: 18,
        start: 0,
        end: 100,
        minValueSpan: 4,
        showDetail: false,
        brushSelect: false,
        borderColor: '#dbe4ed',
        fillerColor: 'rgba(60,103,170,0.1)',
      },
    ],
    series: [
      {
        name: '模拟日K',
        type: 'candlestick',
        data: indexed.map((point) => [point.open, point.close, point.low, point.high]),
        barMaxWidth: 12,
        itemStyle: { color: rise, color0: fall, borderColor: rise, borderColor0: fall },
      },
      ...lines,
      {
        name: '模拟成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        barMaxWidth: 12,
        data: indexed.map((point) => ({
          value: point.volume,
          itemStyle: { color: point.close >= point.open ? rise : fall, opacity: 0.65 },
        })),
      },
    ],
  };
  return (
    <Panel
      className="kline-panel"
      title={preview ? 'K 线效果预览 · 模拟行情' : '交易 K 线 · 演示行情样本'}
      subtitle="场内 ETF 日线 · 独立虚构开高低收与成交量 · 未复权，非真实市场行情"
      action={action}
    >
      <div className="kline-identity">
        <div>
          <strong>{fund.name}</strong>
          <span>{code} · 场内 · 模拟数据</span>
        </div>
        <Badge tone="amber">仅用于界面展示</Badge>
      </div>
      <div className="kline-toolbar">
        <Tabs
          value={range}
          onChange={setRange}
          items={[
            { label: '近 1 月', value: 'month' },
            { label: '近 3 月', value: 'quarter' },
            { label: '近 6 月', value: 'half' },
            { label: '近 1 年', value: 'year' },
            { label: '全部', value: 'all' },
            { label: '自定义', value: 'custom' },
          ]}
        />
        <button className="button secondary" onClick={() => setResetKey((value) => value + 1)}>
          重置缩放
        </button>
      </div>
      {range === 'custom' && (
        <div className="kline-dates">
          <label className="field">
            起始日期
            <input
              type="date"
              min={full[0].date}
              max={DATA_DATE}
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </label>
          <label className="field">
            截止日期
            <input
              type="date"
              min={full[0].date}
              max={DATA_DATE}
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </label>
          {invalid && (
            <p role="alert" className="market-error">
              请选择 {full[0].date} 至 {DATA_DATE} 内有效的起止日期。
            </p>
          )}
        </div>
      )}
      {last ? (
        <>
          <dl className="kline-quote" aria-label="所选范围末日模拟行情">
            <div className="kline-close">
              <dt>{last.date} 收盘</dt>
              <dd>
                {price(last.close)} <small>元</small>
              </dd>
            </div>
            <div>
              <dt>较前一工作日</dt>
              <dd className={change != null && change >= 0 ? 'positive' : 'negative'}>
                {change == null ? '—' : `${signed(change)}%`}
              </dd>
            </div>
            <div>
              <dt>开盘 / 元</dt>
              <dd>{price(last.open)}</dd>
            </div>
            <div>
              <dt>最高 / 元</dt>
              <dd>{price(last.high)}</dd>
            </div>
            <div>
              <dt>最低 / 元</dt>
              <dd>{price(last.low)}</dd>
            </div>
            <div>
              <dt>成交量 / 万份</dt>
              <dd>{(last.volume / 10000).toFixed(2)}</dd>
            </div>
          </dl>
          <div className="kline-legend" role="group" aria-label="K线图例与均线显示">
            <span className="kline-candle-key">
              <i style={{ background: rise }} />
              阳线：收盘 ≥ 开盘
            </span>
            <span className="kline-candle-key">
              <i style={{ background: fall }} />
              阴线：收盘 &lt; 开盘
            </span>
            {averages.map((item) => (
              <label key={item.days} style={{ color: item.color }}>
                <input
                  type="checkbox"
                  checked={enabled.includes(item.days)}
                  onChange={(event) =>
                    setEnabled((values) =>
                      event.target.checked
                        ? [...values, item.days]
                        : values.filter((value) => value !== item.days),
                    )
                  }
                />
                MA{item.days}
                <span className={`kline-line kline-line-${item.line}`} />
              </label>
            ))}
          </div>
          <Chart
            key={`${code}-${range}-${start}-${end}-${resetKey}`}
            option={option}
            label={`${fund.name}虚构开高低收行情：${indexed.length}根日K蜡烛、成交量和可切换均线，非真实市场行情`}
            height={405}
          />
          <div className="kline-caption">
            <p>
              {indexed[0].date} 至 {last.date} · {indexed.length}{' '}
              个模拟工作日样本。悬停查看开高低收，拖动底部滑块缩放；日期按钮和均线勾选也支持键盘。均线仅按样本收盘价计算。
            </p>
            {preview && (
              <Link className="button secondary" to={`/funds/${code}`}>
                查看这只 ETF 的详情 →
              </Link>
            )}
          </div>
        </>
      ) : (
        <EmptyState
          title="所选范围没有可用模拟行情"
          description="请调整日期范围；示例仅跳过周末，不作为真实交易日历。"
        />
      )}
    </Panel>
  );
}
