import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Stat, Tabs } from '../../shared/ui';
import { sourceLabel } from '../../shared/source-label';
import {
  apiRequest,
  formatDateTime,
  formatMoney,
  jsonRequest,
  localDate,
  requestKey,
} from '../../shared/backend';
import './portfolio.css';

type Account = {
  account_id: string;
  name: string;
  base_currency: string;
  status: string;
  opened_at: string;
};

type LedgerEvent = {
  event_id: string;
  account_id: string;
  event_type: EventType;
  fund_code: string | null;
  amount: string | null;
  units: string | null;
  business_date: string;
  confirmation_date: string | null;
  settlement_date: string | null;
  channel: string;
  confirmed_fee: string | null;
  state: 'confirmed' | 'pending' | 'unknown';
  source: string;
  note: string | null;
  recorded_at: string;
  is_current: boolean;
  correction: null | { action: 'reverse' | 'replace'; reason: string };
};

type Snapshot = {
  account: Account;
  cutoff: string;
  cash: { currency: string; confirmed: string; available: string; pending_settlement: string };
  positions: Array<{
    fund_code: string;
    fund_name: string | null;
    confirmed_units: string;
    cost_history_complete: boolean;
  }>;
  pending_events: LedgerEvent[];
  unknown_events: LedgerEvent[];
  coverage: {
    cash_and_units: string;
    cost_history: string;
    settlement_history: string;
    market_valuation: string;
  };
  generated_at: string;
};
type PositionValuation = {
  status: 'loading' | 'ready' | 'missing' | 'error';
  date?: string;
  price?: number;
  marketValue?: number;
  source?: string | null;
  kind?: 'price' | 'nav';
};
type PortfolioCondition = {
  revision_id: string;
  account_id: string;
  version: number;
  effective_date: string;
  risk_level: 'conservative' | 'balanced' | 'aggressive';
  max_single_position_pct: string;
  investment_horizon_days: number;
  channel: string;
  planned_additional_cash: string | null;
  no_holdings_confirmed: boolean;
  recorded_at: string;
};
type FundDetail = {
  series?: {
    kind: 'price' | 'nav' | null;
    source_id: string | null;
    rows: Array<{ date: string; close?: string | null; unit_nav?: string | null }>;
  } | null;
};

type EventType = 'cash_in' | 'cash_out' | 'initial_position' | 'buy' | 'sell' | 'dividend';
type EventDraft = {
  event_type: EventType;
  fund_code: string;
  amount: string;
  units: string;
  business_date: string;
  confirmation_date: string;
  settlement_date: string;
  channel: string;
  confirmed_fee: string;
  state: 'confirmed' | 'pending' | 'unknown';
  source: string;
  note: string;
};

const eventNames: Record<EventType, string> = {
  cash_in: '现金转入',
  cash_out: '现金转出',
  initial_position: '初始持仓',
  buy: '买入 / 加仓',
  sell: '卖出 / 减仓 / 清仓',
  dividend: '现金分红',
};
const stateNames = { confirmed: '已确认', pending: '待确认', unknown: '待核对' } as const;
const tabs = [
  { label: '持仓总览', value: 'overview' },
  { label: '账本记录', value: 'ledger' },
  { label: '投资条件', value: 'conditions' },
];

function freshEvent(type: EventType = 'cash_in'): EventDraft {
  const today = localDate();
  return {
    event_type: type,
    fund_code: '',
    amount: '',
    units: '',
    business_date: today,
    confirmation_date: today,
    settlement_date: today,
    channel: '手工录入',
    confirmed_fee: type === 'buy' || type === 'sell' ? '0' : '',
    state: 'confirmed',
    source: 'user_confirmed',
    note: '',
  };
}

function isFundEvent(type: EventType) {
  return ['initial_position', 'buy', 'sell', 'dividend'].includes(type);
}

function errorMessage(cause: unknown) {
  return cause instanceof Error ? cause.message : '操作失败，请稍后重试。';
}

export function RealPortfolioPage() {
  const [params, setParams] = useSearchParams();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState('');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<LedgerEvent[]>([]);
  const [condition, setCondition] = useState<PortfolioCondition | null>(null);
  const [valuations, setValuations] = useState<Record<string, PositionValuation>>({});
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [accountEditor, setAccountEditor] = useState(false);
  const [accountName, setAccountName] = useState('我的投资账户');
  const [eventEditor, setEventEditor] = useState<EventDraft | null>(null);
  const [eventRequestKey, setEventRequestKey] = useState('');
  const [linkDecisionId, setLinkDecisionId] = useState('');
  const [reverseTarget, setReverseTarget] = useState<LedgerEvent | null>(null);
  const [reverseReason, setReverseReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [conditionEditor, setConditionEditor] = useState(false);
  const [conditionDraft, setConditionDraft] = useState({
    effective_date: localDate(),
    risk_level: 'balanced' as PortfolioCondition['risk_level'],
    max_single_position_pct: '25',
    investment_horizon_days: '365',
    channel: '本人确认',
    planned_additional_cash: '',
    no_holdings_confirmed: false,
  });
  const tab = tabs.some((item) => item.value === params.get('tab'))
    ? params.get('tab')!
    : 'overview';
  const requestedAccountId = params.get('account');

  const loadAccounts = useCallback(async (preferred?: string) => {
    const result = await apiRequest<{ items: Account[] }>('/api/v1/portfolio/accounts');
    setAccounts(result.items);
    setAccountId((current) => {
      const requested = preferred || current;
      return result.items.some((item) => item.account_id === requested)
        ? requested
        : result.items[0]?.account_id || '';
    });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setStatus('loading');
    apiRequest<{ items: Account[] }>('/api/v1/portfolio/accounts', {}, controller.signal)
      .then((result) => {
        setAccounts(result.items);
        setAccountId(result.items.some((item) => item.account_id === requestedAccountId)
          ? requestedAccountId || ''
          : result.items[0]?.account_id || '');
        setStatus('ready');
      })
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(errorMessage(cause));
        setStatus('error');
      });
    return () => controller.abort();
  }, [requestedAccountId]);

  const refreshAccount = useCallback(async () => {
    if (!accountId) {
      setSnapshot(null);
      setEvents([]);
      return;
    }
    setStatus('loading');
    setError('');
    try {
      const query = new URLSearchParams({ account_id: accountId });
      const cutoff = new URLSearchParams({ account_id: accountId, cutoff: localDate() });
      const [eventResult, snapshotResult, conditionResult] = await Promise.all([
        apiRequest<{ items: LedgerEvent[] }>(`/api/v1/ledger/events?${query}`),
        apiRequest<Snapshot>(`/api/v1/portfolio/snapshots?${cutoff}`),
        apiRequest<{ current: PortfolioCondition | null }>(`/api/v1/portfolio/conditions?${query}`),
      ]);
      setEvents(eventResult.items);
      setSnapshot(snapshotResult);
      setCondition(conditionResult.current);
      setStatus('ready');
    } catch (cause) {
      setError(errorMessage(cause));
      setStatus('error');
    }
  }, [accountId]);

  useEffect(() => {
    void refreshAccount();
  }, [refreshAccount]);

  useEffect(() => {
    const positions = snapshot?.positions ?? [];
    const controller = new AbortController();
    if (!positions.length) {
      setValuations({});
      return () => controller.abort();
    }
    setValuations(Object.fromEntries(positions.map((item) => [item.fund_code, { status: 'loading' as const }])));
    void Promise.all(
      positions.map(async (position) => {
        try {
          const detail = await apiRequest<FundDetail>(
            `/api/funds/${encodeURIComponent(position.fund_code)}`,
            {},
            controller.signal,
          );
          const rows = detail.series?.rows ?? [];
          const latest = [...rows].reverse().find((row) => {
            const value = detail.series?.kind === 'price' ? row.close : row.unit_nav;
            return value != null && Number.isFinite(Number(value)) && Number(value) > 0;
          });
          if (!latest || !detail.series?.kind) return [position.fund_code, { status: 'missing' as const }] as const;
          const price = Number(detail.series.kind === 'price' ? latest.close : latest.unit_nav);
          return [position.fund_code, {
            status: 'ready' as const,
            date: latest.date,
            price,
            marketValue: price * Number(position.confirmed_units),
            source: detail.series.source_id,
            kind: detail.series.kind,
          }] as const;
        } catch (cause) {
          if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
          return [position.fund_code, { status: 'error' as const }] as const;
        }
      }),
    ).then((entries) => {
      if (!controller.signal.aborted) setValuations(Object.fromEntries(entries));
    }).catch((cause) => {
      if (!(cause instanceof DOMException && cause.name === 'AbortError')) setValuations({});
    });
    return () => controller.abort();
  }, [snapshot]);

  const activeEvents = useMemo(() => events.filter((item) => item.is_current), [events]);
  const valuationSummary = useMemo(() => {
    const items = snapshot?.positions ?? [];
    const ready = items.map((item) => valuations[item.fund_code]).filter((item) => item?.status === 'ready');
    const marketValue = ready.reduce((sum, item) => sum + (item.marketValue ?? 0), 0);
    const complete = items.length === ready.length;
    const cash = Number(snapshot?.cash.confirmed ?? 0);
    return { marketValue, complete, total: complete ? cash + marketValue : null };
  }, [snapshot, valuations]);
  const openEvent = (type: EventType = 'cash_in', fundCode = '') => {
    const draft = freshEvent(type);
    draft.fund_code = fundCode;
    setEventEditor(draft);
    setEventRequestKey(requestKey('ledger'));
    setLinkDecisionId('');
    setError('');
    setMessage('');
  };

  useEffect(() => {
    const action = params.get('action');
    if (!accountId || eventEditor || (action !== 'buy' && action !== 'sell')) return;
    const draft = freshEvent(action);
    draft.fund_code = params.get('fund') || '';
    setEventEditor(draft);
    setEventRequestKey(requestKey('decision-execution'));
    setLinkDecisionId(params.get('decision') || '');
    const next = new URLSearchParams(params);
    next.delete('action');
    next.delete('decision');
    setParams(next, { replace: true });
  }, [accountId, eventEditor, params, setParams]);

  const createAccount = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      const created = await apiRequest<Account>(
        '/api/v1/portfolio/accounts',
        jsonRequest({ name: accountName.trim(), base_currency: 'CNY' }, requestKey('account')),
      );
      await loadAccounts(created.account_id);
      setAccountId(created.account_id);
      setAccountEditor(false);
      setMessage('账户已建立。请先记录可用现金或已有持仓。');
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  const openCondition = () => {
    setConditionDraft({
      effective_date: localDate(),
      risk_level: condition?.risk_level ?? 'balanced',
      max_single_position_pct: condition?.max_single_position_pct ?? '25',
      investment_horizon_days: String(condition?.investment_horizon_days ?? 365),
      channel: condition?.channel ?? '本人确认',
      planned_additional_cash: condition?.planned_additional_cash ?? '',
      no_holdings_confirmed: condition?.no_holdings_confirmed ?? false,
    });
    setConditionEditor(true);
    setError('');
  };

  const saveCondition = async (event: FormEvent) => {
    event.preventDefault();
    if (!accountId) return;
    setSaving(true);
    setError('');
    try {
      await apiRequest(
        '/api/v1/portfolio/conditions',
        jsonRequest(
          {
            account_id: accountId,
            effective_date: conditionDraft.effective_date,
            risk_level: conditionDraft.risk_level,
            max_single_position_pct: conditionDraft.max_single_position_pct,
            investment_horizon_days: Number(conditionDraft.investment_horizon_days),
            channel: conditionDraft.channel.trim(),
            planned_additional_cash: conditionDraft.planned_additional_cash || null,
            no_holdings_confirmed: conditionDraft.no_holdings_confirmed,
          },
          requestKey('condition'),
        ),
      );
      setConditionEditor(false);
      setMessage('投资条件已作为新版本保存；旧版本仍保留用于历史复核。');
      await refreshAccount();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  const saveEvent = async (event: FormEvent) => {
    event.preventDefault();
    if (!eventEditor || !accountId) return;
    setSaving(true);
    setError('');
    const type = eventEditor.event_type;
    const isTrade = type === 'buy' || type === 'sell';
    const hasAmount = type !== 'initial_position';
    const hasUnits = type === 'initial_position' || isTrade;
    const payload = {
      account_id: accountId,
      event_type: type,
      fund_code: isFundEvent(type) ? eventEditor.fund_code.trim() || null : null,
      amount: hasAmount ? eventEditor.amount || null : null,
      units: hasUnits ? eventEditor.units || null : null,
      business_date: eventEditor.business_date,
      confirmation_date:
        eventEditor.state === 'confirmed' ? eventEditor.confirmation_date || null : null,
      settlement_date:
        eventEditor.state === 'confirmed' ? eventEditor.settlement_date || null : null,
      channel: eventEditor.channel.trim(),
      confirmed_fee: isTrade ? eventEditor.confirmed_fee || null : null,
      state: eventEditor.state,
      source: eventEditor.source.trim(),
      note: eventEditor.note.trim() || null,
    };
    try {
      const key = eventRequestKey || requestKey(linkDecisionId ? 'decision-execution' : 'ledger');
      if (linkDecisionId) {
        const linkRole = eventEditor.state === 'confirmed' ? 'actual_execution' : 'planned_execution';
        await apiRequest(
          `/api/v1/strategy/decisions/${encodeURIComponent(linkDecisionId)}/executions`,
          jsonRequest({ ...payload, role: linkRole }, key),
        );
      } else {
        await apiRequest<LedgerEvent>(
          '/api/v1/ledger/events',
          jsonRequest(payload, key),
        );
      }
      setEventEditor(null);
      setEventRequestKey('');
      setLinkDecisionId('');
      setMessage(
        linkDecisionId
          ? eventEditor.state === 'confirmed'
            ? '实际成交已写入账本，并已关联到原决定。'
            : '待确认记录已写入账本，并作为计划执行关联到原决定；确认后请再记录实际成交。'
          : type === 'sell'
          ? '卖出记录已写入账本。卖出全部确认份额时即形成清仓。'
          : '记录已写入正式账本。',
      );
      await refreshAccount();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  const reverseEvent = async (event: FormEvent) => {
    event.preventDefault();
    if (!reverseTarget) return;
    setSaving(true);
    setError('');
    try {
      await apiRequest(
        `/api/v1/ledger/events/${encodeURIComponent(reverseTarget.event_id)}/corrections`,
        jsonRequest({ action: 'reverse', reason: reverseReason.trim() }, requestKey('correction')),
      );
      setReverseTarget(null);
      setReverseReason('');
      setMessage('误录已通过反向更正失效，原始记录仍保留用于审计。');
      await refreshAccount();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  const fillExitUnits = () => {
    if (!eventEditor) return;
    const position = snapshot?.positions.find(
      (item) => item.fund_code === eventEditor.fund_code.trim(),
    );
    if (!position) {
      setError('当前账户没有这只基金的已确认份额，无法自动填写清仓份额。');
      return;
    }
    setEventEditor({ ...eventEditor, units: position.confirmed_units });
    setMessage('已填入全部确认份额；请按实际成交确认金额和费用后保存。');
  };

  return (
    <div className="portfolio-page">
      <PageHeader
        eyebrow="PORTFOLIO LEDGER"
        title="我的持仓"
        description="用不可覆盖的正式账本记录现金、持仓和实际交易。误录通过更正保留审计轨迹。"
        actions={
          <>
            <button className="button secondary" onClick={() => setAccountEditor(true)}>
              新建账户
            </button>
            <button className="button primary" disabled={!accountId} onClick={() => openEvent()}>
              ＋ 记录实际变动
            </button>
          </>
        }
      />
      {accounts.length > 0 && (
        <div className="portfolio-context">
          <label className="field portfolio-account-select">
            <span>当前账户</span>
            <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
              {accounts.map((account) => (
                <option key={account.account_id} value={account.account_id}>
                  {account.name}
                </option>
              ))}
            </select>
          </label>
          <span className="muted">数据保存在本机数据库，刷新页面后仍会保留。</span>
        </div>
      )}
      {message && <Notice title="已完成">{message}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {status === 'loading' && <div className="page-loading" role="status">正在核对正式账本…</div>}
      {status !== 'loading' && accounts.length === 0 && (
        <Panel>
          <EmptyState
            title="还没有投资账户"
            description="建立账户后，可以依次记录现金、已有持仓和后续成交。"
            action={<button className="button primary" onClick={() => setAccountEditor(true)}>建立第一个账户</button>}
          />
        </Panel>
      )}
      {accountId && snapshot && (
        <>
          <Tabs
            items={tabs}
            value={tab}
            onChange={(value) => {
              const next = new URLSearchParams(params);
              next.set('tab', value);
              setParams(next);
            }}
          />
          {tab === 'overview' && (
            <>
              <div className="portfolio-stats">
                <Stat label="可用现金" value={formatMoney(snapshot.cash.available)} detail={`已确认 ${formatMoney(snapshot.cash.confirmed)} · 待结算 ${formatMoney(snapshot.cash.pending_settlement)}`} />
                <Stat label="最新持仓估值" value={snapshot.positions.length && !valuationSummary.complete ? '待补全' : formatMoney(valuationSummary.marketValue)} detail={valuationSummary.complete ? '按各基金最新真实价格或净值' : '有基金缺少可用序列，不按零计算'} />
                <Stat label="估算总资产" value={formatMoney(valuationSummary.total)} detail={valuationSummary.total == null ? '估值不完整，暂不合计' : `${snapshot.positions.length} 只基金 + 已确认现金`} />
                <Stat label="待处理事件" value={`${snapshot.pending_events.length + snapshot.unknown_events.length} 笔`} detail={`待确认 ${snapshot.pending_events.length} · 待核对 ${snapshot.unknown_events.length}`} />
              </div>
              {snapshot.positions.length > 0 && !valuationSummary.complete && (
                <Notice tone="warning" title="当前市值尚未进入账本快照">
                  账本可靠展示确认现金和份额；页面只对取得真实价格或净值的基金估值，缺失项目不会按零合计。
                </Notice>
              )}
              <Panel
                title="已确认持仓"
                subtitle="清仓通过卖出全部确认份额完成；误录撤销使用账本更正，两者含义不同。"
                action={<button className="button secondary" onClick={() => openEvent('initial_position')}>录入已有持仓</button>}
              >
                {!snapshot.positions.length ? (
                  <EmptyState
                    title="当前没有已确认持仓"
                    description="如果账户已有基金，可以录入初始持仓；如果准备开始投资，请先记录现金转入。"
                    action={<button className="button primary" onClick={() => openEvent('cash_in')}>记录现金转入</button>}
                  />
                ) : (
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr><th>基金</th><th>确认份额</th><th>最新真实估值</th><th>成本历史</th><th>操作</th></tr></thead>
                      <tbody>
                        {snapshot.positions.map((position) => (
                          <tr key={position.fund_code}>
                            <td><Link to={`/funds/${position.fund_code}`}>{position.fund_name || position.fund_code}</Link><small>{position.fund_code}</small></td>
                            <td className="numeric">{position.confirmed_units}</td>
                            <td>{valuations[position.fund_code]?.status === 'ready' ? formatMoney(valuations[position.fund_code].marketValue) : valuations[position.fund_code]?.status === 'loading' ? '读取中…' : '未取得'}<small>{valuations[position.fund_code]?.status === 'ready' ? `${valuations[position.fund_code].date} · ${valuations[position.fund_code].kind === 'price' ? '收盘价' : '单位净值'} · ${sourceLabel(valuations[position.fund_code].source, '来源未标注')}` : '不按零计算'}</small></td>
                            <td><Badge tone={position.cost_history_complete ? 'green' : 'amber'}>{position.cost_history_complete ? '完整' : '初始成本不完整'}</Badge></td>
                            <td><div className="toolbar portfolio-row-actions">
                              <button className="button small secondary" onClick={() => openEvent('buy', position.fund_code)}>加仓</button>
                              <button className="button small secondary" onClick={() => openEvent('sell', position.fund_code)}>减仓 / 清仓</button>
                            </div></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            </>
          )}
          {tab === 'ledger' && (
            <Panel title="正式账本记录" subtitle="更正不会删除原始事件；只有当前有效记录参与余额计算。">
              <div className="toolbar">
                {(['cash_in', 'cash_out', 'initial_position', 'buy', 'sell', 'dividend'] as EventType[]).map((type) => (
                  <button key={type} className="button secondary small" onClick={() => openEvent(type)}>{eventNames[type]}</button>
                ))}
              </div>
              {!events.length ? (
                <EmptyState title="还没有账本记录" description="从现金转入或已有持仓开始记录。" />
              ) : (
                <div className="table-wrap">
                  <table className="data-table">
                    <thead><tr><th>业务日期</th><th>类型</th><th>基金 / 渠道</th><th>金额 / 份额</th><th>状态</th><th>操作</th></tr></thead>
                    <tbody>
                      {[...events].reverse().map((item) => (
                        <tr key={item.event_id} className={!item.is_current ? 'portfolio-reversed-row' : ''}>
                          <td>{item.business_date}<small>记录于 {formatDateTime(item.recorded_at)}</small></td>
                          <td>{eventNames[item.event_type]}</td>
                          <td>{item.fund_code || '现金'}<small>{item.channel}</small></td>
                          <td>{item.amount ? formatMoney(item.amount) : '—'}<small>{item.units ? `${item.units} 份` : '无份额变动'}</small></td>
                          <td>{item.is_current ? <Badge tone={item.state === 'confirmed' ? 'green' : 'amber'}>{stateNames[item.state]}</Badge> : <Badge tone="neutral">已更正</Badge>}</td>
                          <td>{item.is_current ? <button className="button danger small" onClick={() => { setReverseTarget(item); setReverseReason(''); }}>更正误录</button> : <span className="muted">{item.correction?.reason || '原记录已失效'}</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="muted">当前有效 {activeEvents.length} 笔，共保留 {events.length} 笔原始记录。</p>
            </Panel>
          )}
          {tab === 'conditions' && (
            <Panel
              title="当前投资条件"
              subtitle="策略使用截止日之前最新的一版条件；每次修订都会追加版本。"
              action={<button className="button primary" onClick={openCondition}>{condition ? '修订投资条件' : '填写投资条件'}</button>}
            >
              {!condition ? (
                <EmptyState title="尚未确认投资条件" description="缺少风险等级、单只仓位上限和投资期限时，策略会明确返回等待。" action={<button className="button primary" onClick={openCondition}>现在填写</button>} />
              ) : (
                <>
                  <dl className="portfolio-condition-grid">
                    <div><dt>生效日期</dt><dd>{condition.effective_date}</dd></div>
                    <div><dt>风险等级</dt><dd>{{ conservative: '稳健', balanced: '均衡', aggressive: '进取' }[condition.risk_level]}</dd></div>
                    <div><dt>单只基金上限</dt><dd>{condition.max_single_position_pct}%</dd></div>
                    <div><dt>计划持有期限</dt><dd>{condition.investment_horizon_days} 天</dd></div>
                    <div><dt>计划追加资金</dt><dd>{formatMoney(condition.planned_additional_cash)}</dd></div>
                    <div><dt>无持仓确认</dt><dd>{condition.no_holdings_confirmed ? '已确认当前无持仓' : '未确认'}</dd></div>
                  </dl>
                  <p className="muted">版本 {condition.version} · {condition.channel} · 记录于 {formatDateTime(condition.recorded_at)}</p>
                </>
              )}
            </Panel>
          )}
        </>
      )}

      {accountEditor && (
        <Modal title="建立投资账户" onClose={() => setAccountEditor(false)} dirty={Boolean(accountName.trim())}>
          <form onSubmit={createAccount}>
            <label className="field"><span>账户名称</span><input value={accountName} maxLength={200} onChange={(event) => setAccountName(event.target.value)} required autoFocus /></label>
            <p className="muted">首版以人民币记账。账户及账本事件写入后不会被覆盖。</p>
            <div className="portfolio-form-actions"><button type="button" className="button secondary" onClick={() => setAccountEditor(false)}>取消</button><button className="button primary" disabled={saving || !accountName.trim()}>{saving ? '正在建立…' : '建立账户'}</button></div>
          </form>
        </Modal>
      )}
      {eventEditor && (
        <Modal title="记录账户变动" onClose={() => { setEventEditor(null); setEventRequestKey(''); }} wide dirty>
          <form onSubmit={saveEvent}>
            <Notice>{linkDecisionId ? '本次记录来自已保存的个人决定；账本事实与决定关联会一次完成，失败时不会留下半笔成交。' : '这里记录已经发生或需要核对的事实，不会连接交易渠道，也不会自动下单。'}</Notice>
            <div className="form-grid">
              <label className="field"><span>记录类型</span><select value={eventEditor.event_type} disabled={Boolean(linkDecisionId)} onChange={(event) => setEventEditor(freshEvent(event.target.value as EventType))}>{Object.entries(eventNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label className="field"><span>确认状态</span><select value={eventEditor.state} onChange={(event) => setEventEditor({ ...eventEditor, state: event.target.value as EventDraft['state'] })}><option value="confirmed">已确认</option><option value="pending">待确认</option>{!linkDecisionId && <option value="unknown">待核对</option>}</select></label>
              {isFundEvent(eventEditor.event_type) && <label className="field"><span>六位基金代码</span><input value={eventEditor.fund_code} inputMode="numeric" pattern="[0-9]{6}" maxLength={6} readOnly={Boolean(linkDecisionId)} onChange={(event) => setEventEditor({ ...eventEditor, fund_code: event.target.value.replace(/\D/g, '').slice(0, 6) })} required /></label>}
              {eventEditor.event_type !== 'initial_position' && <label className="field"><span>{eventEditor.event_type === 'dividend' ? '现金分红金额' : '确认金额'}</span><input type="number" min="0.000001" step="0.000001" value={eventEditor.amount} onChange={(event) => setEventEditor({ ...eventEditor, amount: event.target.value })} required={eventEditor.state === 'confirmed'} /></label>}
              {['initial_position', 'buy', 'sell'].includes(eventEditor.event_type) && <label className="field"><span>确认份额</span><div className="portfolio-inline-input"><input type="number" min="0.000001" step="0.000001" value={eventEditor.units} onChange={(event) => setEventEditor({ ...eventEditor, units: event.target.value })} required={eventEditor.state === 'confirmed' || eventEditor.event_type === 'initial_position'} />{eventEditor.event_type === 'sell' && <button type="button" className="button secondary small" onClick={fillExitUnits}>填入全部份额</button>}</div></label>}
              {(eventEditor.event_type === 'buy' || eventEditor.event_type === 'sell') && <label className="field"><span>确认费用（免费填 0）</span><input type="number" min="0" step="0.000001" value={eventEditor.confirmed_fee} onChange={(event) => setEventEditor({ ...eventEditor, confirmed_fee: event.target.value })} required={eventEditor.state === 'confirmed'} /></label>}
              <label className="field"><span>业务日期</span><input type="date" value={eventEditor.business_date} onChange={(event) => setEventEditor({ ...eventEditor, business_date: event.target.value })} required /></label>
              <label className="field"><span>确认日期</span><input type="date" min={eventEditor.business_date} value={eventEditor.confirmation_date} onChange={(event) => setEventEditor({ ...eventEditor, confirmation_date: event.target.value })} required={eventEditor.state === 'confirmed'} disabled={eventEditor.state !== 'confirmed'} /></label>
              <label className="field"><span>结算日期</span><input type="date" min={eventEditor.confirmation_date || eventEditor.business_date} value={eventEditor.settlement_date} onChange={(event) => setEventEditor({ ...eventEditor, settlement_date: event.target.value })} disabled={eventEditor.state !== 'confirmed'} /></label>
              <label className="field"><span>渠道</span><input value={eventEditor.channel} maxLength={200} onChange={(event) => setEventEditor({ ...eventEditor, channel: event.target.value })} required /></label>
              <label className="field"><span>事实来源</span><input value={eventEditor.source} maxLength={200} onChange={(event) => setEventEditor({ ...eventEditor, source: event.target.value })} required /></label>
              <label className="field"><span>备注</span><textarea value={eventEditor.note} maxLength={1000} onChange={(event) => setEventEditor({ ...eventEditor, note: event.target.value })} /></label>
            </div>
            {error && <p role="alert" className="portfolio-error">{error}</p>}
            <div className="portfolio-form-actions"><button type="button" className="button secondary" onClick={() => { setEventEditor(null); setEventRequestKey(''); }}>取消</button><button className="button primary" disabled={saving}>{saving ? '正在写入…' : '写入正式账本'}</button></div>
          </form>
        </Modal>
      )}
      {reverseTarget && (
        <Modal title="更正误录" onClose={() => setReverseTarget(null)} dirty={Boolean(reverseReason)}>
          <form onSubmit={reverseEvent}>
            <Notice tone="warning" title="原始记录不会被删除">本次操作会追加一条反向更正，使原事件不再参与余额计算。策略中的清仓则是卖出全部份额。</Notice>
            <label className="field"><span>更正原因</span><textarea value={reverseReason} maxLength={1000} onChange={(event) => setReverseReason(event.target.value)} required autoFocus /></label>
            <div className="portfolio-form-actions"><button type="button" className="button secondary" onClick={() => setReverseTarget(null)}>取消</button><button className="button danger" disabled={saving || !reverseReason.trim()}>{saving ? '正在更正…' : '确认更正误录'}</button></div>
          </form>
        </Modal>
      )}
      {conditionEditor && (
        <Modal title="投资条件" onClose={() => setConditionEditor(false)} wide dirty>
          <form onSubmit={saveCondition}>
            <Notice>条件用于限制策略仓位和新增风险；保存后不会自动创建交易。</Notice>
            <div className="form-grid">
              <label className="field"><span>生效日期</span><input type="date" value={conditionDraft.effective_date} onChange={(event) => setConditionDraft({ ...conditionDraft, effective_date: event.target.value })} required /></label>
              <label className="field"><span>风险等级</span><select value={conditionDraft.risk_level} onChange={(event) => setConditionDraft({ ...conditionDraft, risk_level: event.target.value as PortfolioCondition['risk_level'] })}><option value="conservative">稳健（组合风险仓位上限 35%）</option><option value="balanced">均衡（组合风险仓位上限 60%）</option><option value="aggressive">进取（组合风险仓位上限 80%）</option></select></label>
              <label className="field"><span>单只基金仓位上限（%）</span><input type="number" min="0.01" max="100" step="0.01" value={conditionDraft.max_single_position_pct} onChange={(event) => setConditionDraft({ ...conditionDraft, max_single_position_pct: event.target.value })} required /></label>
              <label className="field"><span>计划持有期限（天）</span><input type="number" min="1" max="36500" step="1" value={conditionDraft.investment_horizon_days} onChange={(event) => setConditionDraft({ ...conditionDraft, investment_horizon_days: event.target.value })} required /></label>
              <label className="field"><span>计划追加资金（可留空）</span><input type="number" min="0" step="0.01" value={conditionDraft.planned_additional_cash} onChange={(event) => setConditionDraft({ ...conditionDraft, planned_additional_cash: event.target.value })} /></label>
              <label className="field"><span>确认渠道</span><input value={conditionDraft.channel} maxLength={200} onChange={(event) => setConditionDraft({ ...conditionDraft, channel: event.target.value })} required /></label>
            </div>
            <label className="portfolio-checkbox"><input type="checkbox" checked={conditionDraft.no_holdings_confirmed} onChange={(event) => setConditionDraft({ ...conditionDraft, no_holdings_confirmed: event.target.checked })} />我已核对并确认当前没有任何基金持仓</label>
            {error && <p role="alert" className="portfolio-error">{error}</p>}
            <div className="portfolio-form-actions"><button type="button" className="button secondary" onClick={() => setConditionEditor(false)}>取消</button><button className="button primary" disabled={saving}>{saving ? '正在保存…' : '保存新版本'}</button></div>
          </form>
        </Modal>
      )}
    </div>
  );
}
