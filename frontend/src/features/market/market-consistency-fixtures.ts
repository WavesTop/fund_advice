/** Synthetic API fixtures only; no source claims or live market requests. */
export function marketDetailFixture() {
  const dates = ['2026-09-11', '2026-09-14', '2026-09-15'];
  const price = {
    kind: 'price' as const,
    source_id: 'fixture.price',
    policy_version: 'fixture-unadjusted',
    updated_at: '2026-09-15T10:00:00Z',
    rows: dates.map((date) => ({
      date,
      open: '10',
      high: '12',
      low: '9',
      close: '11',
      volume: '100',
      amount: null,
    })),
  };
  const nav = {
    kind: 'nav' as const,
    source_id: 'fixture.nav',
    policy_version: 'fixture-nav',
    updated_at: '2026-09-15T10:00:00Z',
    rows: [
      { date: dates[0], unit_nav: '1.02', accumulated_nav: '1.20' },
      { date: dates[2], unit_nav: '1.04', accumulated_nav: '1.22' },
    ],
  };
  const market = {
    code: '000016',
    name: '一致性测试指数',
    kind: 'index' as const,
    source_id: 'fixture.index',
    relation_status: 'linked' as const,
    relation_source_id: 'fixture.prospectus',
    verified_at: '2026-09-15T10:00:00Z',
    evidence_url: 'https://example.test/prospectus',
    rows: dates.map((date) => ({
      date,
      open: '100',
      high: '105',
      low: '99',
      close: '103',
      volume: '1000',
      amount: null,
    })),
  };
  return {
    fund: {
      share_id: 'fixture-share',
      code: '510050',
      name: '一致性测试ETF',
      fund_type: 'ETF',
      source_id: 'fixture.catalog',
    },
    series: price,
    series_options: [price, nav],
    related_market: market,
    related_markets: [market],
  };
}

export function failedRefreshFixture() {
  const previous = marketDetailFixture();
  return {
    error: {
      code: 'fund_refresh_failed',
      message: '刷新失败，关系核验状态已同步。',
      details: {
        refresh: { status: 'failed', stages: {} },
        current: {
          ...previous,
          related_market: null,
          related_markets: [
            {
              ...previous.related_market,
              relation_status: 'withheld',
              relation_reason: '本轮关系核验失败',
              rows: [],
            },
          ],
        },
      },
    },
  };
}
