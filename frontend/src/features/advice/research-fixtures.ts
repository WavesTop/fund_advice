/** Synthetic fixtures for automated checks only. Never imported by production UI. */
import type { ResearchItem, ResearchResponse } from './research-model';

export function researchFixture(): ResearchResponse {
  const item: ResearchItem = {
    code: 'BK_TEST', name: '测试行业', source_id: 'fixture.board', universe_type: 'hot_board',
    as_of: '2026-09-15', updated_at: '2026-09-15T10:00:00Z', funds: [],
    periods: (['short', 'medium', 'long'] as const).map((id) => ({
      id, name: { short: '短期', medium: '中期', long: '长期' }[id],
      range: { short: '约 1 周至 1 个月', medium: '约 1 至 3 个月', long: '约 3 至 6 个月' }[id],
      lookback_sessions: { short: 20, medium: 60, long: 120 }[id],
      status: 'strong', label: '上涨走势', reason: '测试价格高于窗口均线',
      return_pct: 4, max_drawdown_pct: -3,
      opportunity: { status: 'insufficient', label: '研究依据不足', summary: '测试：只有历史行情，尚缺经营证据。',
        supports: ['测试历史表现'], challenges: ['测试反证保留'], conditions: ['补充经营资料'], missing: ['尚缺已核验经营与估值'] },
      strength: { eligible: true, rank: 1, percentile: 100, sample_count: 4, reason: '' },
    })),
  };
  return {
    method_version: 'fixture-screen-v1', generated_at: '2026-09-15T12:00:00Z', items: [item],
    universe: { label: '合成测试行业池', scope: 'verified_industry_all', ranking_as_of: '2026-09-15', collected_count: 1, failed_count: 0 },
    advantages: (['short', 'medium', 'long'] as const).map((id) => ({ id, name: id, horizon: '测试期限', eligible_count: 4,
      message: '仅用于观察排序的测试样本', items: [{ code: item.code, name: item.name, label: '测试领先', reason: '固定测试条件' }] })),
  };
}

export function linkedFixture(): ResearchResponse {
  const data = researchFixture();
  const item = data.items[0];
  item.code = 'IDX_TEST';
  item.name = '测试参考指数';
  item.source_id = 'fixture.index';
  item.universe_type = 'tracked_index';
  item.funds = [{ code: '000001', name: '测试关联基金' }];
  item.research = {
    version: 'research-view-v1', subject_key: 'tracked_index:fixture.index:IDX_TEST',
    operation_status: 'unavailable', operation_reason: '测试：无个人操作',
    periods: item.periods.map((period) => ({ id: period.id, evidence_state: 'insufficient', market_available: true,
      comparison_available: true, label: '测试：依据不足', gaps: ['测试缺项'], recommendation_status: 'not_evaluated', recommendation_reason: '测试：未择优' })),
    fund_associations: [{ code: '000001', name: '测试关联基金', status: 'linked', source_id: 'fixture.prospectus',
      evidence_url: 'https://example.org/test-prospectus', verified_at: '2026-09-15T10:00:00Z', limitations: ['仅合成关联，不是推荐'] }],
  };
  data.advantages = [];
  return data;
}
