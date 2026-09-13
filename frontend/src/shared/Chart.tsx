import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart, CandlestickChart, BarChart, PieChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  MarkLineComponent,
} from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';

echarts.use([
  LineChart,
  CandlestickChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  MarkLineComponent,
  SVGRenderer,
]);
export function Chart({
  option,
  label,
  height = 300,
}: {
  option: EChartsOption;
  label: string;
  height?: number;
}) {
  const element = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.EChartsType | null>(null);
  useEffect(() => {
    if (!element.current) return;
    const chart = echarts.init(element.current, undefined, { renderer: 'svg' });
    instance.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      chart.dispose();
      instance.current = null;
    };
  }, []);
  useEffect(() => {
    instance.current?.setOption(
      {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        ...option,
        aria: { enabled: true, description: label },
      },
      true,
    );
  }, [option, label]);
  return (
    <div
      ref={element}
      className="chart"
      role="img"
      aria-label={label}
      style={{ height, width: '100%' }}
    />
  );
}
