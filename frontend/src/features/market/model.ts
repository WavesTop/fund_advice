import { funds } from '../../shared/data';
import type { Fund } from '../../shared/types';

export interface MarketFund extends Fund {
  aliases?: string[];
  historical?: boolean;
}

export const marketFunds: MarketFund[] = [
  ...funds.map((fund) => ({
    ...fund,
    aliases: fund.code === '000001' ? ['示例稳健成长混合'] : [],
  })),
  {
    code: '009999',
    name: '示例历史精选混合',
    share: 'A类',
    type: '混合型',
    sector: '大消费',
    nav: '1.0836',
    change: 0,
    coverage: '部分资料',
    chart: 'nav',
    color: '#8b919f',
    historical: true,
  },
];

export interface FundFilters {
  query: string;
  type: string;
  sector: string;
  coverage: string;
  sort: string;
  onlyHeld: boolean;
}

export function filterFunds(items: MarketFund[], filters: FundFilters, heldCodes: Set<string>) {
  const query = filters.query.trim().toLowerCase();
  return items
    .filter(
      (fund) =>
        (!query ||
          [fund.code, fund.name, ...(fund.aliases ?? [])].some((text) =>
            text.toLowerCase().includes(query),
          )) &&
        (!filters.type || fund.type === filters.type) &&
        (!filters.sector || fund.sector === filters.sector) &&
        (!filters.coverage || fund.coverage === filters.coverage) &&
        (!filters.onlyHeld || heldCodes.has(fund.code)),
    )
    .sort((a, b) => {
      if (query && (a.code === query || b.code === query)) return a.code === query ? -1 : 1;
      if (filters.sort === 'change')
        return Number(!!a.historical) - Number(!!b.historical) || b.change - a.change;
      if (filters.sort === 'name')
        return a.name.localeCompare(b.name, 'zh-CN') || a.code.localeCompare(b.code);
      return a.code.localeCompare(b.code);
    });
}

export function validateFundCode(code: string) {
  return /^\d{6}$/.test(code.trim()) ? '' : '请输入 6 位数字基金代码。';
}

export function resolvePage(rawPage: string | null, count: number, pageSize: number) {
  const page = Number(rawPage);
  const max = Math.max(1, Math.ceil(count / pageSize));
  return Number.isInteger(page) && page > 0 ? Math.min(page, max) : 1;
}

export type MarketPoint = {
  date: string;
  nav: number | null;
  ohlc: [number, number, number, number];
};

// Explicitly fictional fixtures, not prices inferred from a fund's net asset value.
export function makeSeries(gaps = false): MarketPoint[] {
  const points: MarketPoint[] = [];
  const start = new Date('2025-09-11T00:00:00Z');
  for (let day = 0; day <= 365; day++) {
    const date = new Date(start.getTime() + day * 86_400_000);
    if (date.getUTCDay() === 0 || date.getUTCDay() === 6) continue;
    const value = 1.005 + day * 0.00046 + Math.sin(day / 18) * 0.031 + Math.sin(day / 4) * 0.008;
    const open = 3.86 + day * 0.0011 + Math.sin(day / 13) * 0.075;
    const close = open + Math.sin(day / 3) * 0.031;
    points.push({
      date: date.toISOString().slice(0, 10),
      nav: gaps && day >= 339 && day <= 342 ? null : Number(value.toFixed(4)),
      ohlc: [open, close, Math.min(open, close) - 0.017, Math.max(open, close) + 0.019],
    });
  }
  return points;
}

export function restrictSeries(points: MarketPoint[], range: string, start: string, end: string) {
  if (range === 'custom')
    return start && end && start <= end
      ? points.filter((point) => point.date >= start && point.date <= end)
      : [];
  const days =
    ({ month: 31, quarter: 93, half: 186, year: 366, all: Infinity } as Record<string, number>)[
      range
    ] ?? 93;
  const cutoff = Date.parse('2026-09-11') - days * 86_400_000;
  return points.filter((point) => Date.parse(point.date) >= cutoff);
}

export const sectorDefinitions = [
  {
    name: '先进制造',
    kind: '行业',
    change: 1.42,
    summary: '从设备更新与高端制造，观察中期需求的持续性。',
    risk: '订单兑现不及预期；短期波动放大。',
    color: '#477c76',
    exposure: 32.6,
  },
  {
    name: '半导体',
    kind: '行业',
    change: 2.14,
    summary: '景气修复带来关注度，仍需跟踪盈利与估值的匹配。',
    risk: '估值敏感；行业库存与订单变化。',
    color: '#7061a3',
    exposure: 68.4,
  },
  {
    name: '红利低波',
    kind: '主题',
    change: 0.36,
    summary: '现金分配与低波动特征，为较长持有期提供观察方向。',
    risk: '风格切换；分红与现金流可能变化。',
    color: '#aa8349',
    exposure: 72.5,
  },
  {
    name: '医药健康',
    kind: '行业',
    change: -0.68,
    summary: '业绩分化较大，样本信息仍不足以支持方向性判断。',
    risk: '研发失败、政策与盈利兑现的不确定性。',
    color: '#a06d80',
    exposure: 46.2,
  },
  {
    name: '清洁能源',
    kind: '主题',
    change: -1.05,
    summary: '净值历史不完整，需先补齐资料再进行趋势评估。',
    risk: '价格竞争；主题暴露有交叉。',
    color: '#59838c',
    exposure: 28.8,
  },
  {
    name: '大消费',
    kind: '行业',
    change: 0.24,
    summary: '观察需求恢复能否转化为稳定的现金流。',
    risk: '需求与渠道库存变化。',
    color: '#9b7e58',
    exposure: 54.7,
  },
  {
    name: '宽基指数',
    kind: '主题',
    change: 1.26,
    summary: '覆盖多行业，观察整体盈利与风险偏好的变化。',
    risk: '市场系统性风险；大权重行业集中。',
    color: '#476db0',
    exposure: 82.3,
  },
];
