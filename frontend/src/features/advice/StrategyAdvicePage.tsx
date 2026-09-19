import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Tabs } from '../../shared/ui';
import {
  apiRequest,
  formatDateTime,
  formatMoney,
  formatPlainPercent,
  jsonRequest,
  localDate,
  requestKey,
} from '../../shared/backend';
import './advice.css';

type Account = { account_id: string; name: string; base_currency: string };
type Action = 'BUY' | 'ADD' | 'HOLD' | 'REDUCE' | 'EXIT' | 'WAIT';
type RecommendationItem = {
  fund_code: string | null;
  fund_name: string | null;
  action: Action;
  target_weight_pct: string | null;
  current_weight_pct: string | null;
  current_value: string | null;
  proposed_amount: string | null;
  proposed_units?: string | null;
  deferred_target_weight_pct?: string | null;
  reason_code: string;
  reason: string;
  metrics?: Record<string, string | number | null>;
  data_quality?: string | Record<string, unknown>;
};
type Recommendation = {
  recommendation_id: string;
  account_id: string;
  strategy_version: string;
  input_cutoff: string;
  data_as_of: string;
  created_at: string;
  status: 'actionable' | 'partial' | 'blocked';
  output: {
    items: RecommendationItem[];
    diagnostics?: string[];
    limitations?: string[];
    blockers?: string[];
    warnings?: string[];
    requested_cutoff?: string;
    expected_market_session?: string;
    latest_available_data_date?: string;
    valuation?: {
      currency: string;
      confirmed_cash: string;
      available_cash: string;
      pending_settlement_cash: string;
      known_market_value: string;
      known_total_assets: string;
      complete: boolean;
      unvalued_fund_codes: string[];
      stale_fund_codes: string[];
    };
    execution_policy?: {
      earliest_execution_session?: string | null;
      sizing?: string;
      fee_basis?: string;
      ordering?: string;
      automatic_ordering?: boolean;
    };
    portfolio?: Record<string, unknown>;
  };
};
type RecommendationFreshness = {
  state: 'current' | 'stale' | 'unavailable';
  reason_code: string;
  reason: string;
  checked_cutoff: string;
  stored_input_hash: string | null;
  current_input_hash: string | null;
  changed_inputs: string[];
};
type TradeLink = {
  link_id: string;
  event_id: string;
  role: 'planned_execution' | 'actual_execution' | 'independent_reference';
  linked_at: string;
  event: {
    event_type: 'buy' | 'sell';
    fund_code: string;
    amount: string | null;
    units: string | null;
    confirmed_fee: string | null;
    business_date: string;
    confirmation_date: string | null;
    settlement_date: string | null;
    state: 'confirmed' | 'pending' | 'unknown';
    corrected: boolean;
  };
};
type Decision = {
  decision_id: string;
  recommendation_id: string;
  account_id: string;
  fund_code: string | null;
  proposed_action: Action;
  choice: 'accepted' | 'partially_accepted' | 'rejected' | 'deferred';
  planned_amount: string | null;
  planned_units?: string | null;
  reason: string;
  created_at: string;
  trade_links?: TradeLink[];
  execution?: {
    status: 'not_required' | 'awaiting_execution' | 'planned' | 'partially_executed' | 'executed' | 'over_executed' | 'execution_invalidated';
    actual_amount: string | null;
    actual_units: string | null;
    actual_fee: string | null;
    actual_event_count: number;
    planned_event_count: number;
    invalidated_actual_event_count: number;
  };
};

const actionMeta: Record<Action, { label: string; tone: 'blue' | 'green' | 'amber' | 'red' | 'neutral'; explanation: string }> = {
  BUY: { label: '买入', tone: 'red', explanation: '当前无持仓且信号达到建仓条件。' },
  ADD: { label: '加仓', tone: 'red', explanation: '已有持仓，目标仓位高于当前仓位。' },
  HOLD: { label: '持有', tone: 'green', explanation: '维持当前仓位，暂不产生交易。' },
  REDUCE: { label: '减仓', tone: 'amber', explanation: '降低部分仓位，保留剩余持仓。' },
  EXIT: { label: '清仓', tone: 'red', explanation: '卖出全部确认份额，使目标仓位降为零。' },
  WAIT: { label: '等待', tone: 'neutral', explanation: '资料或条件不足，暂不执行交易。' },
};
const choiceNames = {
  accepted: '采纳',
  partially_accepted: '部分采纳',
  rejected: '不采纳',
  deferred: '暂缓',
} as const;
const statusNames = { actionable: '可执行', partial: '部分可执行', blocked: '暂不可执行' } as const;
const executionNames = {
  not_required: '无需成交',
  awaiting_execution: '等待实际成交',
  planned: '已有待确认记录',
  partially_executed: '已有部分实际成交',
  executed: '实际成交已关联',
  over_executed: '实际成交超过计划',
  execution_invalidated: '原成交已更正',
} as const;

function messageOf(cause: unknown) {
  return cause instanceof Error ? cause.message : '请求失败，请稍后重试。';
}

function dataQualityText(value: RecommendationItem['data_quality']) {
  if (!value) return '未提供';
  if (typeof value === 'string') return value;
  const entries = Object.entries(value);
  return entries.length ? entries.map(([key, item]) => `${key}: ${String(item)}`).join('；') : '未提供';
}

export function StrategyAdvicePage() {
  const [params, setParams] = useSearchParams();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState('');
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
  const [freshness, setFreshness] = useState<RecommendationFreshness | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [decisionItem, setDecisionItem] = useState<RecommendationItem | null>(null);
  const [choice, setChoice] = useState<Decision['choice']>('accepted');
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const tab = params.get('tab') === 'decisions' ? 'decisions' : 'current';

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<{ items: Account[] }>('/api/v1/portfolio/accounts', {}, controller.signal)
      .then((result) => {
        setAccounts(result.items);
        setAccountId(result.items[0]?.account_id || '');
        setStatus('ready');
      })
      .catch((cause) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(messageOf(cause));
        setStatus('error');
      });
    return () => controller.abort();
  }, []);

  const loadAccount = useCallback(async () => {
    if (!accountId) {
      setRecommendation(null);
      setFreshness(null);
      setDecisions([]);
      return;
    }
    setStatus('loading');
    setError('');
    const accountQuery = new URLSearchParams({ account_id: accountId });
    const latestQuery = new URLSearchParams({ account_id: accountId, cutoff: localDate() });
    try {
      const [latest, history] = await Promise.all([
        apiRequest<{ item: Recommendation | null; freshness: RecommendationFreshness }>(`/api/v1/strategy/recommendations/latest?${latestQuery}`),
        apiRequest<{ items: Decision[] } | Decision[]>(`/api/v1/strategy/decisions?${accountQuery}`),
      ]);
      setRecommendation(latest.item);
      setFreshness(latest.freshness);
      setDecisions(Array.isArray(history) ? history : history.items);
      setStatus('ready');
    } catch (cause) {
      setError(messageOf(cause));
      setStatus('error');
    }
  }, [accountId]);

  useEffect(() => {
    void loadAccount();
  }, [loadAccount]);

  const generate = async () => {
    if (!accountId) return;
    setGenerating(true);
    setError('');
    setMessage('');
    try {
      const result = await apiRequest<Recommendation>(
        '/api/v1/strategy/recommendations',
        jsonRequest({ account_id: accountId, cutoff: localDate() }),
      );
      setRecommendation(result);
      await loadAccount();
      setMessage('已使用当前正式账本和本地真实历史序列生成并保存本次建议。');
      setStatus('ready');
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setGenerating(false);
    }
  };

  const openDecision = (item: RecommendationItem) => {
    if (freshness?.state !== 'current') return;
    setDecisionItem(item);
    setChoice('accepted');
    setAmount(item.proposed_amount || '');
    setReason('');
    setError('');
  };

  const saveDecision = async (event: FormEvent) => {
    event.preventDefault();
    if (!decisionItem || !recommendation || freshness?.state !== 'current') return;
    setSaving(true);
    setError('');
    const amountAllowed = ['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(decisionItem.action)
      && ['accepted', 'partially_accepted'].includes(choice);
    if (choice === 'partially_accepted' && (!amount || Number(amount) <= 0)) {
      setError('部分采纳需要填写大于零的调整后计划金额。');
      setSaving(false);
      return;
    }
    if (choice === 'partially_accepted' && decisionItem.proposed_amount
      && Number(amount) >= Number(decisionItem.proposed_amount)) {
      setError('部分采纳金额必须小于系统建议金额；如全部执行请选择“采纳”。');
      setSaving(false);
      return;
    }
    try {
      await apiRequest<Decision>(
        '/api/v1/strategy/decisions',
        jsonRequest(
          {
            recommendation_id: recommendation.recommendation_id,
            account_id: recommendation.account_id,
            cutoff: localDate(),
            fund_code: decisionItem.fund_code,
            proposed_action: decisionItem.action,
            choice,
            planned_amount: amountAllowed ? amount || null : null,
            reason: reason.trim(),
            supersedes_decision_id: null,
          },
          requestKey('decision'),
        ),
      );
      setDecisionItem(null);
      setMessage('你的决定已单独保存。系统建议、个人决定和实际成交仍保持分离。');
      await loadAccount();
      const next = new URLSearchParams(params);
      next.set('tab', 'decisions');
      setParams(next);
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setSaving(false);
    }
  };

  const counts = useMemo(() => {
    const result = Object.fromEntries(Object.keys(actionMeta).map((action) => [action, 0])) as Record<Action, number>;
    for (const item of recommendation?.output.items ?? []) result[item.action] += 1;
    return result;
  }, [recommendation]);
  const recommendationIsCurrent = freshness?.state === 'current';

  return (
    <div className="advice-page strategy-advice-page">
      <PageHeader
        eyebrow="ADVICE & DECISIONS"
        title="建议与决策"
        description="用当前持仓与真实历史数据生成买入、加仓、持有、减仓、清仓或等待建议，再单独记录你的选择。"
        actions={<button className="button primary" disabled={!accountId || generating} onClick={generate}>{generating ? '正在核算…' : '重新生成建议'}</button>}
      />
      {accounts.length > 0 && (
        <div className="advice-context strategy-account-context">
          <label className="field"><span>投资账户</span><select value={accountId} onChange={(event) => setAccountId(event.target.value)}>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.name}</option>)}</select></label>
          <div><span className="muted">建议截止日</span><strong>{recommendation?.input_cutoff || '尚未生成'}</strong></div>
          <div><span className="muted">数据截至</span><strong>{recommendation?.data_as_of || '尚未生成'}</strong></div>
          <div><span className="muted">策略版本</span><strong>{recommendation?.strategy_version || '尚未生成'}</strong></div>
        </div>
      )}
      {message && <Notice title="已完成">{message}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {status === 'loading' && <div className="page-loading" role="status">正在读取正式建议记录…</div>}
      {status !== 'loading' && !accounts.length && (
        <Panel><EmptyState title="请先建立投资账户" description="策略需要现金和持仓作为当前状态，才能计算目标仓位与操作金额。" action={<Link className="button primary" to="/portfolio">前往建立账户</Link>} /></Panel>
      )}
      {!!accounts.length && (
        <>
          <Tabs items={[{ label: '当前建议', value: 'current' }, { label: '决策记录', value: 'decisions' }]} value={tab} onChange={(value) => { const next = new URLSearchParams(params); next.set('tab', value); setParams(next); }} />
          {tab === 'current' && (
            <>
              <Panel title="完整动作口径" subtitle="每个研究对象只给出一种当前动作；无交易也会明确显示。">
                <div className="strategy-action-legend">
                  {(Object.keys(actionMeta) as Action[]).map((action) => <div key={action}><Badge tone={actionMeta[action].tone}>{actionMeta[action].label}</Badge><b>{recommendation ? counts[action] : '—'}</b><span>{actionMeta[action].explanation}</span></div>)}
                </div>
              </Panel>
              {!recommendation ? (
                <Panel><EmptyState title="尚未生成正式策略建议" description="系统会读取当前确认现金、持仓份额和已核验基金历史；缺少资料时给出等待，不填造操作。" action={<button className="button primary" onClick={generate}>生成第一份建议</button>} /></Panel>
              ) : (
                <>
                  <Notice tone={recommendation.status === 'blocked' ? 'warning' : 'info'} title={`本次状态：${statusNames[recommendation.status]}`}>
                    建议生成于 {formatDateTime(recommendation.created_at)}。行情应截至 {recommendation.output.expected_market_session || recommendation.input_cutoff}，实际最新资料截至 {recommendation.output.latest_available_data_date || recommendation.data_as_of}。建议本身不会下单；只有另行记录的真实成交才会改变持仓。
                  </Notice>
                  {freshness?.state === 'stale' && (
                    <Notice tone="warning" title="建议已过期，暂不可执行">
                      {freshness.reason} 请先点击“重新生成建议”。
                      {!!freshness.changed_inputs.length && <> 已变化输入：{freshness.changed_inputs.join('、')}。</>}
                    </Notice>
                  )}
                  {freshness?.state === 'unavailable' && (
                    <Notice tone="warning" title="暂时无法核验建议时效">{freshness.reason} 核验完成前不能记录决定或进入成交。</Notice>
                  )}
                  {recommendation.output.expected_market_session && recommendation.output.latest_available_data_date && recommendation.output.latest_available_data_date < recommendation.output.expected_market_session && <Notice tone="warning" title="行情资料落后于应有交易日">建议已按当前可用资料生成，请补齐行情或净值后重新计算，不要沿用旧金额。</Notice>}
                  {!!recommendation.output.blockers?.length && <Notice tone="warning" title="本次操作被阻止">{recommendation.output.blockers.join('；')}</Notice>}
                  {!!recommendation.output.warnings?.length && <Notice tone="warning" title="需要留意">{recommendation.output.warnings.join('；')}</Notice>}
                  {recommendation.output.valuation && <Panel title="本次组合估值" subtitle={!recommendation.output.valuation.complete ? `未估值基金：${recommendation.output.valuation.unvalued_fund_codes.join('、') || '未提供代码'}` : recommendation.output.valuation.stale_fund_codes?.length ? `估值过期基金：${recommendation.output.valuation.stale_fund_codes.join('、')}` : '已确认持仓均取得应有交易日的真实价格或净值。'}><div className="strategy-valuation"><div><span>可用现金</span><b>{formatMoney(recommendation.output.valuation.available_cash)}</b><small>已确认 {formatMoney(recommendation.output.valuation.confirmed_cash)} · 待结算 {formatMoney(recommendation.output.valuation.pending_settlement_cash)}</small></div><div><span>已知持仓市值</span><b>{formatMoney(recommendation.output.valuation.known_market_value)}</b></div><div><span>可用于策略核算的资产</span><b>{formatMoney(recommendation.output.valuation.known_total_assets)}</b></div><div><span>最早参考执行日</span><b>{recommendation.output.execution_policy?.earliest_execution_session || '尚未确定'}</b></div></div>{recommendation.output.execution_policy?.fee_basis && <p className="muted">{recommendation.output.execution_policy.fee_basis}</p>}{recommendation.output.execution_policy?.ordering && <p className="muted">执行顺序：{recommendation.output.execution_policy.ordering}</p>}</Panel>}
                  <div className="strategy-recommendation-grid">
                    {recommendation.output.items.map((item, index) => (
                      <Panel key={`${item.fund_code || 'cash'}-${index}`} className={`strategy-action-card action-${item.action.toLowerCase()}`} title={<><span>{item.fund_name || item.fund_code || '账户级建议'}</span> <Badge tone={actionMeta[item.action].tone}>{actionMeta[item.action].label}</Badge></>} subtitle={item.fund_code || '账户整体'} action={<button className="button secondary small" disabled={!recommendationIsCurrent} onClick={() => openDecision(item)}>{recommendationIsCurrent ? '记录我的决定' : '重算后再决定'}</button>}>
                        <p className="strategy-reason">{item.reason}</p>
                        <dl className="strategy-weight-grid">
                          <div><dt>当前权重</dt><dd>{formatPlainPercent(item.current_weight_pct)}</dd></div>
                          <div><dt>目标权重</dt><dd>{formatPlainPercent(item.target_weight_pct)}</dd></div>
                          <div><dt>当前估值</dt><dd>{formatMoney(item.current_value)}</dd></div>
                          <div><dt>建议变动金额</dt><dd>{formatMoney(item.proposed_amount)}</dd></div>
                          <div><dt>建议变动份额</dt><dd>{item.proposed_units == null ? '—' : Number(item.proposed_units).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}</dd></div>
                          {item.deferred_target_weight_pct != null && <div><dt>结算后待重算目标</dt><dd>{formatPlainPercent(item.deferred_target_weight_pct)}</dd></div>}
                        </dl>
                        <details><summary>指标与数据质量</summary><p className="muted">原因代码：{item.reason_code}</p><p className="muted">数据质量：{dataQualityText(item.data_quality)}</p>{item.metrics && <dl className="strategy-metrics">{Object.entries(item.metrics).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value == null ? '—' : String(value)}</dd></div>)}</dl>}</details>
                        {item.fund_code && ['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(item.action) && (
                          <p className="muted">先记录并采纳决定；实际成交发生后，再从“决策记录”写入账本并关联。</p>
                        )}
                      </Panel>
                    ))}
                  </div>
                  {!!recommendation.output.diagnostics?.length && <Notice tone="warning" title="本次诊断">{recommendation.output.diagnostics.join('；')}</Notice>}
                  {!!recommendation.output.limitations?.length && <Panel title="适用边界"><ul>{recommendation.output.limitations.map((text) => <li key={text}>{text}</li>)}</ul></Panel>}
                </>
              )}
            </>
          )}
          {tab === 'decisions' && (
            <Panel title="我的决策记录" subtitle="记录个人选择，不自动写入持仓；执行后请把真实成交写入账本。">
              {!decisions.length ? <EmptyState title="还没有保存决定" description="在当前建议中选择一项并记录采纳、部分采纳、不采纳或暂缓。" /> : <div className="table-wrap"><table className="data-table"><thead><tr><th>时间</th><th>基金</th><th>建议</th><th>我的决定</th><th>计划金额 / 份额</th><th>实际成交</th><th>执行状态</th><th>理由</th><th>后续</th></tr></thead><tbody>{decisions.map((item) => {
                const tradeAction = ['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(item.proposed_action);
                const accepted = ['accepted', 'partially_accepted'].includes(item.choice);
                const executionStatus = item.execution?.status || (accepted && tradeAction ? 'awaiting_execution' : 'not_required');
                const canRecordExecution = Boolean(
                  item.fund_code && accepted && tradeAction
                  && executionStatus !== 'executed'
                  && executionStatus !== 'over_executed'
                );
                return <tr key={item.decision_id}><td>{formatDateTime(item.created_at)}</td><td>{item.fund_code || '账户整体'}</td><td><Badge tone={actionMeta[item.proposed_action].tone}>{actionMeta[item.proposed_action].label}</Badge></td><td>{choiceNames[item.choice]}</td><td>{formatMoney(item.planned_amount)}{item.planned_units && <small className="muted">份额 {Number(item.planned_units).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}</small>}</td><td><b>{formatMoney(item.execution?.actual_amount)}</b>{item.execution?.actual_units && <small className="muted">份额 {Number(item.execution.actual_units).toLocaleString('zh-CN', { maximumFractionDigits: 6 })} · 费用 {formatMoney(item.execution.actual_fee)}</small>}</td><td>{executionNames[executionStatus]}</td><td>{item.reason}</td><td>{canRecordExecution ? <Link className="button secondary small" to={`/portfolio?tab=ledger&account=${encodeURIComponent(item.account_id)}&fund=${item.fund_code}&action=${item.proposed_action === 'BUY' || item.proposed_action === 'ADD' ? 'buy' : 'sell'}&decision=${encodeURIComponent(item.decision_id)}`}>{executionStatus === 'partially_executed' ? '继续记录成交' : '记录实际成交'}</Link> : <span className="muted">{executionStatus === 'executed' ? '已完成' : executionStatus === 'over_executed' ? '请复核超额成交' : accepted && tradeAction ? '等待实际成交' : '无需成交'}</span>}</td></tr>;
              })}</tbody></table></div>}
            </Panel>
          )}
        </>
      )}
      {decisionItem && recommendation && (
        <Modal title="记录我的决定" onClose={() => setDecisionItem(null)} dirty={Boolean(reason || amount)}>
          <form onSubmit={saveDecision}>
            <Notice title={`${decisionItem.fund_name || '账户整体'} · ${actionMeta[decisionItem.action].label}`}>保存决定不会执行交易，也不会改变账本余额。</Notice>
            <label className="field"><span>我的选择</span><select value={choice} onChange={(event) => { const next = event.target.value as Decision['choice']; setChoice(next); setAmount(next === 'accepted' ? decisionItem.proposed_amount || '' : ''); }}>{Object.entries(choiceNames).filter(([value]) => value !== 'partially_accepted' || ['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(decisionItem.action)).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label className="field"><span>计划金额{choice === 'partially_accepted' ? '（必填且小于建议金额）' : choice === 'accepted' && ['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(decisionItem.action) ? '（按建议金额）' : '（无需填写）'}</span><input type="number" min="0.01" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} required={choice === 'partially_accepted'} disabled={!['BUY', 'ADD', 'REDUCE', 'EXIT'].includes(decisionItem.action) || choice !== 'partially_accepted'} /></label>
            <label className="field"><span>决定理由</span><textarea value={reason} maxLength={1000} onChange={(event) => setReason(event.target.value)} required autoFocus /></label>
            {error && <p className="field-error" role="alert">{error}</p>}
            <div className="form-actions"><button type="button" className="button secondary" onClick={() => setDecisionItem(null)}>取消</button><button className="button primary" disabled={saving || !reason.trim()}>{saving ? '正在保存…' : '保存决定'}</button></div>
          </form>
        </Modal>
      )}
    </div>
  );
}
