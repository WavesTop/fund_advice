import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { RealPortfolioPage } from './portfolio/RealPortfolioPage';
import { StrategyAdvicePage } from './advice/StrategyAdvicePage';
import { RealReviewPage } from './review/RealReviewPage';

vi.mock('../shared/Chart', () => ({
  Chart: ({ label }: { label: string }) => <div role="img" aria-label={label} />,
}));

const account = {
  account_id: 'acct-1',
  name: '长期账户',
  base_currency: 'CNY',
  status: 'active',
  opened_at: '2026-09-19T08:00:00Z',
};
const currentFreshness = {
  state: 'current' as const,
  reason_code: 'inputs_match',
  reason: '建议输入与当前输入一致。',
  checked_cutoff: '2026-09-19',
  stored_input_hash: 'same-hash',
  current_input_hash: 'same-hash',
  changed_inputs: [],
};

function json(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }));
}

function renderRoute(path: string, element: React.ReactNode) {
  return render(<RouterProvider router={createMemoryRouter([{ path: path.split('?')[0], element }], { initialEntries: [path] })} />);
}

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('正式交易流程', () => {
  it('从建议链接预填买入，并在待确认记录中移除确认与结算日期', async () => {
    let posted: Record<string, unknown> | null = null;
    let linked = false;
    let executionKey = '';
    const otherAccount = { ...account, account_id: 'acct-other', name: '另一个账户' };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [otherAccount, account] });
      if (url.startsWith('/api/v1/ledger/events?')) return json({ account_id: account.account_id, items: [] });
      if (url.startsWith('/api/v1/portfolio/snapshots?')) return json({
        version: 'portfolio-ledger-v1', account, cutoff: '2026-09-19',
        cash: { currency: 'CNY', confirmed: '10000', available: '10000', pending_settlement: '0' }, positions: [],
        pending_events: [], unknown_events: [],
        coverage: { cash_and_units: 'confirmed', cost_history: 'complete', settlement_history: 'complete', market_valuation: 'unavailable' },
        generated_at: '2026-09-19T08:00:00Z',
      });
      if (url.startsWith('/api/v1/portfolio/conditions?')) return json({ account_id: account.account_id, current: null, items: [] });
      if (url === '/api/v1/strategy/decisions/dec-1/executions' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        posted = body;
        linked = body.role === 'planned_execution';
        executionKey = String((init.headers as Record<string, string>)['Idempotency-Key']);
        return json({ event: { event_id: 'evt-1', ...posted }, link: { link_id: 'link-1' } });
      }
      throw new Error(`unexpected ${url}`);
    }));
    const user = userEvent.setup();
    renderRoute('/portfolio?account=acct-1&action=buy&fund=008888&decision=dec-1', <RealPortfolioPage />);
    const dialog = await screen.findByRole('dialog', { name: '记录账户变动' });
    expect(within(dialog).getByLabelText('六位基金代码')).toHaveValue('008888');
    await user.selectOptions(within(dialog).getByLabelText('确认状态'), 'pending');
    await user.click(within(dialog).getByRole('button', { name: '写入正式账本' }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      event_type: 'buy',
      fund_code: '008888',
      state: 'pending',
      confirmation_date: null,
      settlement_date: null,
      role: 'planned_execution',
    });
    await waitFor(() => expect(linked).toBe(true));
    expect(executionKey).toMatch(/^decision-execution-/);
  });

  it('拒绝建议时清空计划金额并只保存个人决定', async () => {
    let posted: Record<string, unknown> | null = null;
    const recommendation = {
      recommendation_id: 'rec-1', account_id: account.account_id,
      strategy_version: 'strategy-v1', input_cutoff: '2026-09-18', data_as_of: '2026-09-17',
      created_at: '2026-09-19T08:00:00Z', status: 'actionable',
      output: {
        expected_market_session: '2026-09-18', latest_available_data_date: '2026-09-17',
        items: [{ fund_code: '008888', fund_name: '真实基金', action: 'BUY', target_weight_pct: '20', current_weight_pct: '0', current_value: '0', proposed_amount: '2000', reason_code: 'BUY', reason: '达到建仓条件', data_quality: { status: 'ready' } }],
        limitations: [], blockers: [], warnings: [],
      },
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [account] });
      if (url.startsWith('/api/v1/strategy/recommendations/latest')) return json({ item: recommendation, freshness: currentFreshness });
      if (url.startsWith('/api/v1/strategy/decisions?')) return json({ account_id: account.account_id, items: [] });
      if (url === '/api/v1/strategy/decisions' && init?.method === 'POST') {
        posted = JSON.parse(String(init.body));
        return json({ decision_id: 'dec-1', created_at: '2026-09-19T08:10:00Z', ...posted });
      }
      throw new Error(`unexpected ${url}`);
    }));
    const user = userEvent.setup();
    renderRoute('/advice', <StrategyAdvicePage />);
    expect(await screen.findByText('行情资料落后于应有交易日')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '记录我的决定' }));
    const dialog = screen.getByRole('dialog', { name: '记录我的决定' });
    await user.selectOptions(within(dialog).getByLabelText('我的选择'), 'rejected');
    expect(within(dialog).getByLabelText('计划金额（无需填写）')).toBeDisabled();
    expect(within(dialog).getByLabelText('计划金额（无需填写）')).toHaveValue(null);
    await user.type(within(dialog).getByLabelText('决定理由'), '估值仍不符合我的要求');
    await user.click(within(dialog).getByRole('button', { name: '保存决定' }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ choice: 'rejected', planned_amount: null, fund_code: '008888', cutoff: expect.any(String) });
  });

  it('策略池为空时仍可保存账户级等待决定', async () => {
    let posted: Record<string, unknown> | null = null;
    const recommendation = {
      recommendation_id: 'rec-wait', account_id: account.account_id,
      strategy_version: 'strategy-v1', input_cutoff: '2026-09-18', data_as_of: '2026-09-18',
      created_at: '2026-09-19T08:00:00Z', status: 'blocked',
      output: {
        expected_market_session: '2026-09-18', latest_available_data_date: '2026-09-18',
        items: [{ fund_code: null, fund_name: null, action: 'WAIT', target_weight_pct: null, current_weight_pct: null, current_value: null, proposed_amount: null, reason_code: 'ELIGIBLE_UNIVERSE_EMPTY', reason: '等待真实资料补齐', data_quality: { status: 'blocked' } }],
        limitations: [], blockers: ['eligible_universe_empty'], warnings: [],
      },
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [account] });
      if (url.startsWith('/api/v1/strategy/recommendations/latest')) return json({ item: recommendation, freshness: currentFreshness });
      if (url.startsWith('/api/v1/strategy/decisions?')) return json({ account_id: account.account_id, items: [] });
      if (url === '/api/v1/strategy/decisions' && init?.method === 'POST') {
        posted = JSON.parse(String(init.body));
        return json({ decision_id: 'dec-wait', created_at: '2026-09-19T08:10:00Z', ...posted });
      }
      throw new Error(`unexpected ${url}`);
    }));
    const user = userEvent.setup();
    renderRoute('/advice', <StrategyAdvicePage />);
    await user.click(await screen.findByRole('button', { name: '记录我的决定' }));
    const dialog = screen.getByRole('dialog', { name: '记录我的决定' });
    await user.selectOptions(within(dialog).getByLabelText('我的选择'), 'deferred');
    await user.type(within(dialog).getByLabelText('决定理由'), '等待真实数据补齐');
    await user.click(within(dialog).getByRole('button', { name: '保存决定' }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ choice: 'deferred', planned_amount: null, fund_code: null, proposed_action: 'WAIT', cutoff: expect.any(String) });
  });

  it('建议输入已经变化时禁用决定与成交入口并要求重算', async () => {
    let latestUrl = '';
    const recommendation = {
      recommendation_id: 'rec-stale', account_id: account.account_id,
      strategy_version: 'strategy-v1', input_cutoff: '2026-09-18', data_as_of: '2026-09-18',
      created_at: '2026-09-18T08:00:00Z', status: 'actionable',
      output: {
        expected_market_session: '2026-09-18', latest_available_data_date: '2026-09-18',
        items: [{ fund_code: '008888', fund_name: '真实基金', action: 'BUY', target_weight_pct: '20', current_weight_pct: '0', current_value: '0', proposed_amount: '2000', reason_code: 'BUY', reason: '达到建仓条件', data_quality: { status: 'ready' } }],
        limitations: [], blockers: [], warnings: [],
      },
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [account] });
      if (url.startsWith('/api/v1/strategy/recommendations/latest')) {
        latestUrl = url;
        return json({
          item: recommendation,
          freshness: {
            ...currentFreshness,
            state: 'stale',
            reason_code: 'inputs_changed',
            reason: '当前账本、约束、行情日期或策略输入已变化。',
            current_input_hash: 'new-hash',
            changed_inputs: ['portfolio_snapshot', 'requested_cutoff'],
          },
        });
      }
      if (url.startsWith('/api/v1/strategy/decisions?')) return json({ account_id: account.account_id, items: [] });
      throw new Error(`unexpected ${url}`);
    }));
    renderRoute('/advice', <StrategyAdvicePage />);
    expect(await screen.findByText('建议已过期，暂不可执行')).toBeInTheDocument();
    expect(latestUrl).toContain('cutoff=');
    expect(screen.getByRole('button', { name: '重算后再决定' })).toBeDisabled();
    expect(screen.getByText('先记录并采纳决定；实际成交发生后，再从“决策记录”写入账本并关联。')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '按实际成交记账' })).not.toBeInTheDocument();
  });

  it('建议跨日后仍允许把实际成交关联到已经采纳的决定', async () => {
    const recommendation = {
      recommendation_id: 'rec-old', account_id: account.account_id,
      strategy_version: 'strategy-v1', input_cutoff: '2026-09-18', data_as_of: '2026-09-18',
      created_at: '2026-09-18T08:00:00Z', status: 'actionable',
      output: { items: [], limitations: [], blockers: [], warnings: [] },
    };
    const decision = {
      decision_id: 'dec-awaiting', recommendation_id: recommendation.recommendation_id,
      account_id: account.account_id, fund_code: '008888', proposed_action: 'BUY', choice: 'accepted',
      planned_amount: '2000', reason: '昨日已经采纳', created_at: '2026-09-18T08:10:00Z',
      planned_units: '1500', trade_links: [],
      execution: { status: 'partially_executed', actual_amount: '1000', actual_units: '750', actual_fee: '1', actual_event_count: 1, planned_event_count: 0, invalidated_actual_event_count: 0 },
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [account] });
      if (url.startsWith('/api/v1/strategy/recommendations/latest')) return json({
        item: recommendation,
        freshness: { ...currentFreshness, state: 'stale', reason_code: 'inputs_changed', reason: '建议已跨日。', changed_inputs: ['requested_cutoff'] },
      });
      if (url.startsWith('/api/v1/strategy/decisions?')) return json({ account_id: account.account_id, items: [decision] });
      throw new Error(`unexpected ${url}`);
    }));
    const user = userEvent.setup();
    renderRoute('/advice?tab=decisions', <StrategyAdvicePage />);
    const row = await screen.findByRole('row', { name: /008888/ });
    expect(within(row).getByText('已有部分实际成交')).toBeInTheDocument();
    expect(within(row).getByRole('link', { name: '继续记录成交' })).toHaveAttribute(
      'href',
      expect.stringContaining('decision=dec-awaiting'),
    );
    expect(within(row).getByRole('link', { name: '继续记录成交' })).toHaveAttribute(
      'href',
      expect.stringContaining('account=acct-1'),
    );
    await user.click(screen.getByRole('button', { name: '当前建议' }));
    expect(screen.getByText('建议已过期，暂不可执行')).toBeInTheDocument();
  });

  it('决定已关联实际成交后分别展示计划与成交并隐藏重复登记入口', async () => {
    const recommendation = {
      recommendation_id: 'rec-current', account_id: account.account_id,
      strategy_version: 'strategy-v1', input_cutoff: '2026-09-19', data_as_of: '2026-09-19',
      created_at: '2026-09-19T08:00:00Z', status: 'actionable',
      output: { items: [], limitations: [], blockers: [], warnings: [] },
    };
    const decision = {
      decision_id: 'dec-executed', recommendation_id: recommendation.recommendation_id,
      account_id: account.account_id, fund_code: '008888', proposed_action: 'BUY', choice: 'accepted',
      planned_amount: '2000', reason: '按建议执行', created_at: '2026-09-19T08:10:00Z',
      trade_links: [{
        link_id: 'link-1', event_id: 'event-1', role: 'actual_execution', linked_at: '2026-09-19T08:20:00Z',
        event: { event_type: 'buy', fund_code: '008888', amount: '1950', units: '1500', confirmed_fee: '1.5', business_date: '2026-09-19', confirmation_date: '2026-09-19', settlement_date: '2026-09-22', state: 'confirmed', corrected: false },
      }],
      execution: { status: 'executed', actual_amount: '1950', actual_units: '1500', actual_fee: '1.5', actual_event_count: 1, planned_event_count: 0, invalidated_actual_event_count: 0 },
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/v1/portfolio/accounts') return json({ items: [account] });
      if (url.startsWith('/api/v1/strategy/recommendations/latest')) return json({ item: recommendation, freshness: currentFreshness });
      if (url.startsWith('/api/v1/strategy/decisions?')) return json({ account_id: account.account_id, items: [decision] });
      throw new Error(`unexpected ${url}`);
    }));
    const user = userEvent.setup();
    renderRoute('/advice', <StrategyAdvicePage />);
    await user.click(await screen.findByRole('button', { name: '决策记录' }));
    const row = screen.getByRole('row', { name: /008888/ });
    expect(within(row).getByText('¥2,000.00')).toBeInTheDocument();
    expect(within(row).getByText('¥1,950.00')).toBeInTheDocument();
    expect(within(row).getByText('实际成交已关联')).toBeInTheDocument();
    expect(within(row).getByText('已完成')).toBeInTheDocument();
    expect(within(row).queryByRole('link', { name: '记录实际成交' })).not.toBeInTheDocument();
  });

  it('旧回测缺少独立验收时不显示为验收通过', async () => {
    const output = {
      version: 'integrated-fund-strategy-v1', status: 'completed', strategy_version: 'strategy-v1',
      start_date: '2021-01-01', end_date: '2026-09-18', input_identity: { common_observations: 900 },
      universe: { items: [{ code: '008888', name: '真实基金', first_date: '2021-01-01', last_date: '2026-09-18', observation_count: 900 }] },
      curves: [
        { id: 'strategy', name: '综合策略', points: [{ date: '2021-01-01', equity: '100000', cash: '100000', market_value: '0', return_pct: '0', drawdown_pct: '0' }, { date: '2026-09-18', equity: '110000', cash: '20000', market_value: '90000', return_pct: '10', drawdown_pct: '-2' }] },
        { id: 'benchmark', name: '持有基准', points: [{ date: '2021-01-01', equity: '100000', cash: '0', market_value: '100000', return_pct: '0', drawdown_pct: '0' }, { date: '2026-09-18', equity: '108000', cash: '0', market_value: '108000', return_pct: '8', drawdown_pct: '-3' }] },
      ],
      metrics: { initial_equity: '100000', final_equity: '110000', total_return_pct: '10', benchmark_return_pct: '8', excess_return_pct: '2', max_drawdown_pct: '-5', total_fees: '100', trade_count: 4, trades_per_fund_year: '1' },
      action_summary: { BUY: 1, ADD: 1, HOLD: 400, REDUCE: 1, EXIT: 1, WAIT: 496 },
      executed_action_summary: { BUY: 1, ADD: 1, REDUCE: 1, EXIT: 1 }, periods: [], trades: [], diagnostics: [], limitations: ['历史结果不保证未来收益。'],
    };
    vi.stubGlobal('fetch', vi.fn(() => json({ item: { run_id: 'run-1', strategy_version: 'strategy-v1', created_at: '2026-09-19T08:00:00Z', start_date: output.start_date, end_date: output.end_date, calendar_basis: 'observed', status: 'completed', output } })));
    renderRoute('/review', <RealReviewPage />);
    expect(await screen.findByText('需要重新验收')).toBeInTheDocument();
    expect(screen.getByText('这份结果缺少独立验收记录')).toBeInTheDocument();
    expect(screen.queryByText('验收通过')).not.toBeInTheDocument();
  });
});
