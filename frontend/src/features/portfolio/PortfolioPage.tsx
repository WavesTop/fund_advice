import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { DATA_DATE, displayTime, funds, getFund, money } from '../../shared/data';
import type { Conditions, Transaction } from '../../shared/types';
import { useDemo, useUnsavedChanges } from '../../shared/store';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Stat, Tabs } from '../../shared/ui';
import {
  appendTransaction,
  findDuplicate,
  isCash,
  isSale,
  recordStatus,
  transactionKinds,
  validateConditions,
  validateTransaction,
  type FieldErrors,
  type TransactionDraft,
} from './model';
import './portfolio.css';
import { FeeRules } from './FeeRules';

const freshDraft = (
  fundCode = '',
  initial = false,
  decisionIds: string[] = [],
): TransactionDraft => ({
  fundCode,
  kind: initial ? '初始持仓' : '买入 / 增仓',
  amount: '',
  shares: '',
  date: '',
  confirmationDate: '',
  settlementDate: '',
  channel: '',
  status: '待确认',
  fee: '',
  note: '',
  decisionIds,
});
const tabs = [
  { label: '持仓总览', value: 'overview' },
  { label: '交易记录', value: 'transactions' },
  { label: '投资条件', value: 'conditions' },
  { label: '交易规则', value: 'fees' },
];

function Field({
  name,
  title,
  error,
  children,
  hint,
}: {
  name: string;
  title: string;
  error?: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="field">
      <label htmlFor={name}>{title}</label>
      {children}
      {hint && <small className="muted">{hint}</small>}
      {error && (
        <small role="alert" id={`${name}-error`} className="portfolio-error">
          {error}
        </small>
      )}
    </div>
  );
}

export function PortfolioPage() {
  const { state, update, notify } = useDemo();
  const [params, setParams] = useSearchParams();
  const selectedTab = tabs.some((item) => item.value === params.get('tab'))
    ? params.get('tab')!
    : 'overview';
  const fundCode = params.get('fund') || '';
  const fund = getFund(fundCode);
  const [editor, setEditor] = useState<TransactionDraft | null>(null);
  const [detail, setDetail] = useState<Transaction | null>(null);
  const [filter, setFilter] = useState('全部状态');
  const [query, setQuery] = useState('');
  const [cancelTarget, setCancelTarget] = useState<Transaction | null>(null);
  const [cancelReason, setCancelReason] = useState('');
  const [showExample, setShowExample] = useState(false);
  const decision = state.decisions.find((item) => item.id === params.get('decision'));
  const transactionId = params.get('transaction');
  useEffect(() => {
    if (transactionId)
      setDetail(state.transactions.find((item) => item.id === transactionId) ?? null);
  }, [transactionId, state.transactions]);
  const closeDetail = () => {
    setDetail(null);
    if (transactionId) {
      const next = new URLSearchParams(params);
      next.delete('transaction');
      setParams(next, { replace: true });
    }
  };

  useEffect(() => {
    const action = params.get('action');
    if (action === 'trade' || action === 'initial') {
      const draft = freshDraft(
        fundCode || decision?.fundCode,
        action === 'initial',
        decision ? [decision.id] : [],
      );
      if (decision?.amount) draft.amount = decision.amount;
      setEditor(draft);
      const next = new URLSearchParams(params);
      next.delete('action');
      setParams(next, { replace: true });
    }
  }, [params, setParams, fundCode, decision]);

  const replaced = new Set(state.transactions.map((item) => item.corrects).filter(Boolean));
  const active = state.transactions.filter(
    (item) => !replaced.has(item.id) && item.status !== '已撤销',
  );
  const fundRecords = active.filter((item) => item.fundCode && !isCash(item.kind));
  const codes = Array.from(new Set(fundRecords.map((item) => item.fundCode)));
  const transactions = [...state.transactions]
    .reverse()
    .filter(
      (item) =>
        (!fundCode || item.fundCode === fundCode) &&
        (filter === '全部状态' || item.status === filter) &&
        (!query ||
          `${item.fundCode} ${getFund(item.fundCode)?.name ?? ''} ${item.note}`.includes(query)),
    );
  const pending = active.filter((item) => item.status !== '已确认');
  const openEditor = (initial = false) =>
    setEditor(freshDraft(fundCode, initial, decision ? [decision.id] : []));
  const switchTab = (value: string) => {
    const next = new URLSearchParams(params);
    next.set('tab', value);
    setParams(next);
  };

  return (
    <div className="portfolio-page">
      <PageHeader
        eyebrow="YOUR PORTFOLIO"
        title="我的持仓"
        description="记录你实际做过的事，让每一次决定都有迹可循。"
        actions={
          <>
            <button className="button secondary" onClick={() => openEditor(true)}>
              录入已有持仓
            </button>
            <button className="button primary" onClick={() => openEditor()}>
              ＋ 记录实际交易
            </button>
          </>
        }
      />
      <Notice title="本地界面演示" tone="info">
        此处保存的都是演示记录。资金占用、可用份额、市值与精确收益等待后端账本核算；保存记录不代表真实交易已经成交。
      </Notice>
      <Tabs items={tabs} value={selectedTab} onChange={switchTab} />
      {state.scenario === 'loading' && (
        <Notice>正在演示资料加载状态，已保存的本地记录仍可查看。</Notice>
      )}
      {state.scenario === 'stale' && (
        <Notice tone="warning">
          示例估值资料已过期，以下记录保留原始填写值，暂不提供当前市值。
        </Notice>
      )}
      {fundCode && (
        <div className="portfolio-context">
          <span>当前基金：{fund ? `${fund.name} · ${fund.code} · ${fund.share}` : fundCode}</span>
          <button
            className="button secondary"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete('fund');
              setParams(next);
            }}
          >
            查看全部基金
          </button>
        </div>
      )}
      {decision && (
        <Notice title="来自个人决定">
          {decision.choice} · {decision.adviceVersion}
          。关联记录仅用于回看，请重新核对实际发生的金额和日期。
          <Link to={`/advice?tab=decisions&decision=${decision.id}`}>返回原决定</Link>
        </Notice>
      )}

      {selectedTab === 'overview' && (
        <>
          <div className="portfolio-stats">
            <Stat label="已记录基金" value={`${codes.length} 只`} detail="仅统计本地演示记录" />
            <Stat label="基金市值" value="—" detail="等待后端估值与份额核算" />
            <Stat
              label="已知现金"
              value={
                state.conditions.cash === '' ? '尚未录入' : `¥ ${money(state.conditions.cash)}`
              }
              detail={
                state.conditions.cash === ''
                  ? '未知不等于零，总资产暂不可用'
                  : '手动填写快照，尚未与交易核对'
              }
            />
            <Stat
              label="待处理记录"
              value={`${pending.length} 笔`}
              detail="含待确认、待补全与待核对"
            />
          </div>
          <Panel
            title={fund ? `${fund.name} · 持仓记录` : '已记录持仓'}
            subtitle="同一基金保留渠道与逐笔记录；精确汇总将在账本接入后提供。"
            action={
              <button className="button secondary" onClick={() => switchTab('transactions')}>
                查看全部交易 →
              </button>
            }
          >
            {codes.length === 0 ? (
              <EmptyState
                title={state.conditions.noHoldings ? '已确认当前没有持仓' : '还没有录入持仓'}
                description={
                  state.conditions.noHoldings
                    ? '你仍可浏览基金与三个周期的方向示例。'
                    : '可以先录入某日的持仓汇总，之后逐步补全原始交易。'
                }
                action={
                  <button className="button primary" onClick={() => openEditor(true)}>
                    录入第一笔持仓
                  </button>
                }
              />
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>基金 / 份额类别</th>
                      <th>渠道</th>
                      <th>有效记录</th>
                      <th>确认份额 / 市值</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {codes
                      .filter((code) => !fundCode || code === fundCode)
                      .map((code) => {
                        const item = getFund(code);
                        const rows = fundRecords.filter((row) => row.fundCode === code);
                        return (
                          <tr key={code}>
                            <td>
                              <Link to={`/funds/${code}`}>{item?.name ?? code}</Link>
                              <small>
                                {code} · {item?.share ?? '待补全'}
                              </small>
                            </td>
                            <td>
                              {Array.from(new Set(rows.map((row) => row.channel))).join('、')}
                            </td>
                            <td>
                              {rows.length} 笔 <Badge tone="amber">待核算</Badge>
                            </td>
                            <td>
                              —<small>逐笔值可查看，尚未计算余额</small>
                            </td>
                            <td>
                              <button
                                className="button secondary"
                                onClick={() => {
                                  const next = new URLSearchParams(params);
                                  next.set('fund', code);
                                  next.set('tab', 'transactions');
                                  setParams(next);
                                }}
                              >
                                查看明细
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
          <div className="grid-two">
            <Panel title="账户资料完整度" subtitle="补全资料后，后续业务分析才能解释适用范围。">
              <ul className="portfolio-checklist">
                <li>
                  <span>持仓范围</span>
                  <Badge tone={codes.length || state.conditions.noHoldings ? 'blue' : 'amber'}>
                    {codes.length
                      ? '已记录部分'
                      : state.conditions.noHoldings
                        ? '确认无持仓'
                        : '尚未录入'}
                  </Badge>
                </li>
                <li>
                  <span>可用现金</span>
                  <Badge tone={state.conditions.cash === '' ? 'amber' : 'blue'}>
                    {state.conditions.cash === '' ? '未知' : '已填写'}
                  </Badge>
                </li>
                <li>
                  <span>风险与期限</span>
                  <Badge
                    tone={state.conditions.risk && state.conditions.horizon ? 'blue' : 'amber'}
                  >
                    {state.conditions.risk && state.conditions.horizon ? '已填写' : '待补全'}
                  </Badge>
                </li>
                <li>
                  <span>交易与费用核算</span>
                  <Badge tone="neutral">等待后端接入</Badge>
                </li>
              </ul>
              <button className="button secondary" onClick={() => switchTab('conditions')}>
                完善投资条件 →
              </button>
            </Panel>
            <Panel title="持仓展示示例" subtitle="独立的虚构样例，不计入上方记录或你的个人持仓。">
              <p className="muted">预览渠道、批次与收益状态如何展示。</p>
              <button className="button secondary" onClick={() => setShowExample(!showExample)}>
                {showExample ? '收起示例' : '查看虚构持仓示例'}
              </button>
              {showExample && (
                <div className="portfolio-example">
                  <Badge tone="amber">虚构数据</Badge>
                  <h3>示例沪深300 ETF · 510300</h3>
                  <p>示例渠道 A · 2 个买入批次</p>
                  <p>展示份额：1,000.0000 份 · 展示成本：¥ 4,000.00</p>
                  <small className="muted">
                    示例数据日期 {DATA_DATE}。以上数字不进入你的账户，收益与费用未计算。
                  </small>
                </div>
              )}
            </Panel>
          </div>
        </>
      )}

      {selectedTab === 'transactions' && (
        <Panel
          title="实际操作记录"
          subtitle="申请时间与记录时间分别保留。所有条目均为演示，原记录可追溯。"
        >
          <div className="toolbar">
            <label className="field">
              搜索记录
              <input
                aria-label="搜索交易记录"
                placeholder="基金名称、代码或备注"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <label className="field">
              确认状态
              <select
                aria-label="筛选交易状态"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              >
                {['全部状态', '待补全', '待确认', '已确认', '待核对', '已撤销'].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <span className="muted">{transactions.length} 笔记录</span>
          </div>
          {transactions.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>申请日期 / 类型</th>
                    <th>基金与渠道</th>
                    <th>金额 / 份额</th>
                    <th>记录状态</th>
                    <th>关联与操作</th>
                  </tr>
                </thead>
                <tbody>
                  {transactions.map((item) => (
                    <tr key={item.id}>
                      <td>
                        {item.date}
                        <small>{item.kind}</small>
                      </td>
                      <td>
                        {isCash(item.kind)
                          ? '现金账本'
                          : getFund(item.fundCode)?.name || item.fundCode}
                        <small>
                          {item.fundCode} {item.channel}
                        </small>
                      </td>
                      <td>
                        {item.amount ? `¥ ${money(item.amount)}` : '金额待补全'}
                        <small>{item.shares ? `${item.shares} 份` : '份额未录入'}</small>
                      </td>
                      <td>
                        <Badge
                          tone={
                            item.status === '已确认'
                              ? 'blue'
                              : item.status === '已撤销'
                                ? 'neutral'
                                : 'amber'
                          }
                        >
                          {item.status}
                        </Badge>
                        {replaced.has(item.id) && <small>已有更正，原记录留痕</small>}
                        {item.corrects && <small>更正 / 撤销记录</small>}
                      </td>
                      <td>
                        <span className="muted">
                          {item.decisionIds.length
                            ? `${item.decisionIds.length} 个关联决定`
                            : '自主记录'}
                        </span>
                        <button className="button secondary" onClick={() => setDetail(item)}>
                          查看记录
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="没有符合条件的交易记录"
              description="调整筛选，或记录一笔实际发生的操作。"
              action={
                <button className="button primary" onClick={() => openEditor()}>
                  记录实际交易
                </button>
              }
            />
          )}
        </Panel>
      )}

      {selectedTab === 'conditions' && (
        <ConditionsForm key={state.conditions.updatedAt} hasRecords={fundRecords.length > 0} />
      )}
      {selectedTab === 'fees' && <FeeRules fundCode={fundCode} />}

      {editor && <TransactionEditor initial={editor} onClose={() => setEditor(null)} />}
      {detail && (
        <Modal title="交易记录详情 · 演示" onClose={closeDetail} wide>
          <div className="portfolio-detail">
            <Badge tone="amber">仅用于界面验证 · 不执行真实交易</Badge>
            <dl>
              <dt>记录对象</dt>
              <dd>
                {getFund(detail.fundCode)?.name ?? '现金账本'} {detail.fundCode} · {detail.channel}
              </dd>
              <dt>操作与状态</dt>
              <dd>
                {detail.kind} · {detail.status}
              </dd>
              <dt>金额 / 份额</dt>
              <dd>
                {detail.amount ? `¥ ${money(detail.amount)}` : '待补全'} /{' '}
                {detail.shares || '待补全'} 份
              </dd>
              <dt>实际确认费用</dt>
              <dd>{detail.fee === '' ? '未知；未应用示例费率' : `¥ ${money(detail.fee)}`}</dd>
              <dt>申请 / 确认 / 到账</dt>
              <dd>
                {detail.date} / {detail.confirmationDate || '待确认'} /{' '}
                {detail.settlementDate || '待补全'}
              </dd>
              <dt>录入时间</dt>
              <dd>{displayTime(detail.createdAt)}</dd>
              <dt>备注及更正原因</dt>
              <dd>{detail.note || '未填写'}</dd>
            </dl>
            {detail.corrects && (
              <p>
                对应原记录：
                <button
                  className="button secondary"
                  onClick={() =>
                    setDetail(
                      state.transactions.find((item) => item.id === detail.corrects) ?? detail,
                    )
                  }
                >
                  查看原始值
                </button>
              </p>
            )}
            {state.transactions
              .filter((item) => item.corrects === detail.id)
              .map((item) => (
                <p key={item.id}>
                  后续记录：
                  <button className="button secondary" onClick={() => setDetail(item)}>
                    查看更正后的值 · {item.status}
                  </button>
                </p>
              ))}
            <h3>关联个人决定</h3>
            {detail.decisionIds.length ? (
              detail.decisionIds.map((id) => (
                <p key={id}>
                  <Link to={`/advice?tab=decisions&decision=${id}`}>
                    查看决定 · {state.decisions.find((item) => item.id === id)?.choice ?? id}
                  </Link>
                </p>
              ))
            ) : (
              <p className="muted">自主交易，尚未关联个人决定。</p>
            )}
            <Notice>
              真实持仓和收益的影响尚未核算。后续业务模块接入后才能提供可用份额、持有时间与费用结果。
            </Notice>
            <div className="portfolio-form-actions">
              {!replaced.has(detail.id) && detail.status !== '已撤销' && (
                <>
                  <button
                    className="button secondary"
                    onClick={() => {
                      setEditor({ ...detail, corrects: detail.id, note: '' });
                      closeDetail();
                    }}
                  >
                    更正记录
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => {
                      setCancelTarget(detail);
                      setCancelReason('');
                      closeDetail();
                    }}
                  >
                    撤销误录
                  </button>
                </>
              )}
              <Link className="button secondary" to="/review?tab=timeline">
                查看决策时间线 →
              </Link>
            </div>
          </div>
        </Modal>
      )}
      {cancelTarget && (
        <Modal
          title="撤销误录并保留历史"
          onClose={() => setCancelTarget(null)}
          dirty={!!cancelReason}
        >
          <p>
            将为 {getFund(cancelTarget.fundCode)?.name ?? '现金账本'} 的 {cancelTarget.date}{' '}
            记录追加撤销记录。原始金额、状态及关联关系仍可查看。
          </p>
          <label className="field">
            撤销原因
            <textarea
              value={cancelReason}
              onChange={(event) => setCancelReason(event.target.value)}
              placeholder="说明为什么这笔记录属于误录"
            />
          </label>
          <div className="portfolio-form-actions">
            <button className="button secondary" onClick={() => setCancelTarget(null)}>
              返回
            </button>
            <button
              className="button primary"
              disabled={!cancelReason.trim()}
              onClick={() => {
                const saved = update((previous) =>
                  appendTransaction(
                    previous,
                    {
                      ...cancelTarget,
                      status: '已撤销',
                      corrects: cancelTarget.id,
                      note: cancelReason.trim(),
                    },
                    crypto.randomUUID(),
                    new Date().toISOString(),
                  ),
                );
                if (saved) {
                  setCancelTarget(null);
                  notify('已追加撤销记录，原始记录和关联均已保留。');
                }
              }}
            >
              确认撤销误录
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function TransactionEditor({
  initial,
  onClose,
}: {
  initial: TransactionDraft;
  onClose: () => void;
}) {
  const { state, update, notify } = useDemo();
  const [draft, setDraft] = useState(initial);
  const [step, setStep] = useState(1);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [duplicateConfirmed, setDuplicateConfirmed] = useState(false);
  const saving = useRef(false);
  const [reason, setReason] = useState('');
  const original = state.transactions.find((item) => item.id === draft.corrects);
  const duplicate = findDuplicate(draft, state.transactions);
  const field = (key: keyof TransactionDraft, value: string) => {
    setDraft((previous) => ({ ...previous, [key]: value }));
    setDuplicateConfirmed(false);
    setErrors((previous) => ({ ...previous, [key]: '' }));
  };
  const advance = () => {
    const validation = validateTransaction(draft);
    const relevant =
      step === 1
        ? Object.fromEntries(
            Object.entries(validation).filter(([key]) =>
              ['kind', 'fundCode', 'channel'].includes(key),
            ),
          )
        : validation;
    if (Object.keys(relevant).length) {
      setErrors(relevant);
      return;
    }
    setErrors({});
    setStep(step + 1);
  };
  const save = () => {
    if (saving.current) return;
    const validation = validateTransaction(draft);
    if (original && !reason.trim()) validation.reason = '更正需要说明原因，以便以后回看。';
    if (duplicate && !duplicateConfirmed)
      validation.duplicate = '请确认这是一笔独立发生的交易，或返回检查原记录。';
    if (Object.keys(validation).length) {
      setErrors(validation);
      return;
    }
    saving.current = true;
    const saved = update((previous) =>
      appendTransaction(
        previous,
        {
          ...draft,
          fundCode: isCash(draft.kind) ? '' : draft.fundCode,
          shares: isCash(draft.kind) ? '' : draft.shares,
          note: `${original ? `更正原因：${reason.trim()}。` : ''}${draft.note}`,
        },
        crypto.randomUUID(),
        new Date().toISOString(),
      ),
    );
    saving.current = false;
    if (saved) {
      notify('演示记录已保存；真实资金与收益尚未核算。');
      onClose();
    }
  };
  return (
    <Modal
      title={
        original ? '更正交易记录' : draft.kind === '初始持仓' ? '录入已有持仓' : '记录实际交易'
      }
      onClose={onClose}
      wide
      dirty={JSON.stringify(draft) !== JSON.stringify(initial) || !!reason}
    >
      <ol className="portfolio-stepper">
        {['确认对象', '填写记录', '核对并保存'].map((label, index) => (
          <li
            key={label}
            className={step === index + 1 ? 'active' : ''}
            aria-current={step === index + 1 ? 'step' : undefined}
          >
            <span>{index + 1}</span>
            {label}
          </li>
        ))}
      </ol>
      <Notice>3.1 界面演示：只在本机保存记录，不计算费用、不成交、不更新真实资产。</Notice>
      {draft.decisionIds.length > 0 && (
        <Notice title="已带入个人计划">
          参考决定已关联；金额仅是待核对草稿，不是成交金额。请按实际交易确认信息填写。
        </Notice>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (step < 3) advance();
          else save();
        }}
        noValidate
      >
        {step === 1 && (
          <div className="grid-two">
            <Field name="trade-kind" title="记录类型" error={errors.kind}>
              <select
                id="trade-kind"
                value={draft.kind}
                onChange={(event) => field('kind', event.target.value)}
              >
                {transactionKinds.map((kind) => (
                  <option key={kind}>{kind}</option>
                ))}
              </select>
            </Field>
            <Field
              name="trade-channel"
              title={isCash(draft.kind) ? '所属资金渠道' : '交易渠道'}
              error={errors.channel}
            >
              <select
                id="trade-channel"
                aria-describedby={errors.channel ? 'trade-channel-error' : undefined}
                value={draft.channel}
                onChange={(event) => field('channel', event.target.value)}
              >
                <option value="">请选择</option>
                {['基金公司直销', '银行', '第三方销售平台', '证券账户', '渠道待补全'].map(
                  (channel) => (
                    <option key={channel}>{channel}</option>
                  ),
                )}
              </select>
            </Field>
            {!isCash(draft.kind) && (
              <Field name="trade-fund" title="基金与份额类别" error={errors.fundCode}>
                <select
                  id="trade-fund"
                  aria-describedby={errors.fundCode ? 'trade-fund-error' : undefined}
                  value={draft.fundCode}
                  onChange={(event) => field('fundCode', event.target.value)}
                >
                  <option value="">请选择基金</option>
                  {funds.map((item) => (
                    <option key={item.code} value={item.code}>
                      {item.code} · {item.name} · {item.share}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {draft.kind === '初始持仓' && (
              <Notice tone="warning" title="初始持仓不是完整交易历史">
                可先录入某日汇总。后续补入历史买入前，需要后端确认它与这条初始记录的关系，以免重复记账。
              </Notice>
            )}
          </div>
        )}
        {step === 2 && (
          <>
            <div className="grid-two">
              <Field
                name="trade-date"
                title={draft.kind === '初始持仓' ? '持仓快照日期' : '申请 / 发生日期'}
                error={errors.date}
              >
                <input
                  id="trade-date"
                  type="date"
                  value={draft.date}
                  aria-describedby={errors.date ? 'trade-date-error' : undefined}
                  onChange={(event) => field('date', event.target.value)}
                />
              </Field>
              <Field name="trade-status" title="已知记录状态">
                <select
                  id="trade-status"
                  value={draft.status}
                  onChange={(event) => field('status', event.target.value)}
                >
                  {['待补全', '待确认', '已确认', '待核对'].map((status) => (
                    <option key={status}>{status}</option>
                  ))}
                </select>
              </Field>
              <Field
                name="trade-amount"
                title={isSale(draft.kind) ? '已知到账金额（可留空）' : '金额 / 已记录成本（元）'}
                error={errors.amount}
              >
                <input
                  id="trade-amount"
                  inputMode="decimal"
                  placeholder="未知请留空"
                  value={draft.amount}
                  aria-describedby={errors.amount ? 'trade-amount-error' : undefined}
                  onChange={(event) => field('amount', event.target.value)}
                />
              </Field>
              {!isCash(draft.kind) && (
                <Field
                  name="trade-shares"
                  title={
                    isSale(draft.kind) || draft.kind === '份额变化'
                      ? '本次份额（必填）'
                      : '已知确认份额'
                  }
                  error={errors.shares}
                >
                  <input
                    id="trade-shares"
                    inputMode="decimal"
                    placeholder="最多四位小数"
                    value={draft.shares}
                    aria-describedby={errors.shares ? 'trade-shares-error' : undefined}
                    onChange={(event) => field('shares', event.target.value)}
                  />
                </Field>
              )}
              <Field
                name="trade-confirmation"
                title="确认日期（可留空）"
                error={errors.confirmationDate}
              >
                <input
                  id="trade-confirmation"
                  type="date"
                  value={draft.confirmationDate}
                  aria-describedby={
                    errors.confirmationDate ? 'trade-confirmation-error' : undefined
                  }
                  onChange={(event) => field('confirmationDate', event.target.value)}
                />
              </Field>
              <Field
                name="trade-settlement"
                title="到账日期（可留空）"
                error={errors.settlementDate}
              >
                <input
                  id="trade-settlement"
                  type="date"
                  value={draft.settlementDate}
                  aria-describedby={errors.settlementDate ? 'trade-settlement-error' : undefined}
                  onChange={(event) => field('settlementDate', event.target.value)}
                />
              </Field>
              <Field
                name="trade-fee"
                title="实际确认费用（元，可留空）"
                error={errors.fee}
                hint="仅填写渠道确认的实际费用；空值表示未知，不会自动使用任何示例费率。"
              >
                <input
                  id="trade-fee"
                  inputMode="decimal"
                  placeholder="未知请留空；确定免费填 0"
                  value={draft.fee}
                  aria-describedby={errors.fee ? 'trade-fee-error' : undefined}
                  onChange={(event) => field('fee', event.target.value)}
                />
              </Field>
            </div>
            <label className="field">
              备注
              <textarea
                value={draft.note}
                onChange={(event) => field('note', event.target.value)}
                placeholder="例如：确认单待收到，或份额变化原因"
              />
            </label>
            {isSale(draft.kind) && (
              <Notice tone="warning">
                可用份额校验尚未接入，卖出记录将保存为“待核对”，不计入已确认账本。请补全初始持仓和历史买入，等待后续核对。
              </Notice>
            )}
          </>
        )}
        {step === 3 && (
          <>
            <dl className="portfolio-confirmation">
              <dt>基金 / 渠道</dt>
              <dd>
                {isCash(draft.kind)
                  ? '现金账本'
                  : `${getFund(draft.fundCode)?.name} · ${draft.fundCode} · ${getFund(draft.fundCode)?.share}`}{' '}
                / {draft.channel}
              </dd>
              <dt>类型与日期</dt>
              <dd>
                {draft.kind} · {draft.date}
              </dd>
              <dt>金额 / 份额</dt>
              <dd>
                {draft.amount ? `¥ ${money(draft.amount)}` : '待补全'} / {draft.shares || '待补全'}{' '}
                份
              </dd>
              <dt>实际费用</dt>
              <dd>{draft.fee === '' ? '未提供，不自动估算' : `¥ ${money(draft.fee)}`}</dd>
              <dt>保存后的状态</dt>
              <dd>
                <Badge tone="amber">{recordStatus(draft)}</Badge> · 真实核算待后端
              </dd>
            </dl>
            {recordStatus(draft) !== draft.status && (
              <Notice tone="warning">
                已根据缺失资料或待核对事项调整保存状态，尚不能认定为已确认账本。
              </Notice>
            )}
            <Panel
              title="交易规则核对"
              subtitle={`${getFund(draft.fundCode)?.share || '现金'} · ${draft.channel}`}
            >
              <p>适用规则：未接入 · 来源与核对日期：待补全。</p>
              <p className="muted">
                没有经过确认的费率规则，本次不试算费用。实际确认费用与未来费用预估分别记录。
              </p>
            </Panel>
            {original && (
              <>
                <Notice title="更正影响">
                  原记录：{original.date} ·{' '}
                  {original.amount ? `¥ ${money(original.amount)}` : '金额未知'} ·{' '}
                  {original.shares || '未知'} 份 · {original.status}
                  。新值见上方摘要。保存后追加更正记录，原值和关联保持可追溯；持仓与收益的影响待核算。
                </Notice>
                <Field name="correction-reason" title="更正原因（必填）" error={errors.reason}>
                  <textarea
                    id="correction-reason"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                  />
                </Field>
              </>
            )}
            {duplicate && (
              <Notice tone="warning" title="发现疑似重复交易">
                <p>
                  已有同基金、同渠道、同日期、同金额和份额的记录。原记录时间：
                  {displayTime(duplicate.createdAt)}。
                </p>
                <label className="portfolio-checkbox">
                  <input
                    type="checkbox"
                    checked={duplicateConfirmed}
                    onChange={(event) => setDuplicateConfirmed(event.target.checked)}
                  />
                  确认这是另一笔独立发生的交易
                </label>
              </Notice>
            )}
            {errors.duplicate && (
              <p className="portfolio-error" role="alert">
                {errors.duplicate}
              </p>
            )}
          </>
        )}
        <div className="portfolio-form-actions">
          {step > 1 && (
            <button className="button secondary" type="button" onClick={() => setStep(step - 1)}>
              上一步
            </button>
          )}
          <button className="button primary" type="submit">
            {step < 3 ? '下一步' : original ? '保存更正记录' : '保存演示记录'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ConditionsForm({ hasRecords }: { hasRecords: boolean }) {
  const { state, update, notify } = useDemo();
  const [draft, setDraft] = useState<Conditions>(state.conditions);
  const [errors, setErrors] = useState<FieldErrors>({});
  const dirty = JSON.stringify(draft) !== JSON.stringify(state.conditions);
  useUnsavedChanges(dirty);
  const setValue = (key: keyof Conditions, value: string | boolean) =>
    setDraft((previous) => ({ ...previous, [key]: value }));
  return (
    <Panel title="投资条件" subtitle="这些是你主动填写的条件；未知值留空，不会替你选择偏好。">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const nextErrors = validateConditions(draft);
          setErrors(nextErrors);
          if (Object.keys(nextErrors).length) return;
          const saved = update((previous) => ({
            ...previous,
            conditions: { ...draft, updatedAt: new Date().toISOString() },
          }));
          if (saved) notify('投资条件已保存。旧建议保留原条件，新建议需要重新评估。');
        }}
        noValidate
      >
        <div className="grid-two">
          <Field
            name="condition-cash"
            title="已知可用现金（元）"
            error={errors.cash}
            hint="当前现金余额；尚不清楚请留空，确定没有现金可填 0。"
          >
            <input
              id="condition-cash"
              inputMode="decimal"
              value={draft.cash}
              placeholder="尚未录入"
              aria-describedby={errors.cash ? 'condition-cash-error' : undefined}
              onChange={(event) => setValue('cash', event.target.value)}
            />
          </Field>
          <Field
            name="condition-additional"
            title="未来可追加资金（元）"
            error={errors.additional}
            hint="未来愿意投入的金额，与当前现金分开。"
          >
            <input
              id="condition-additional"
              inputMode="decimal"
              value={draft.additional}
              placeholder="尚未录入"
              aria-describedby={errors.additional ? 'condition-additional-error' : undefined}
              onChange={(event) => setValue('additional', event.target.value)}
            />
          </Field>
          <Field name="condition-risk" title="风险要求">
            <select
              id="condition-risk"
              value={draft.risk}
              onChange={(event) => setValue('risk', event.target.value)}
            >
              <option value="">尚未选择</option>
              <option>优先控制波动</option>
              <option>平衡波动与增长</option>
              <option>可接受较大波动</option>
            </select>
          </Field>
          <Field
            name="condition-concentration"
            title="单只基金集中度上限（%）"
            error={errors.concentration}
          >
            <input
              id="condition-concentration"
              inputMode="decimal"
              placeholder="例如 20；未知留空"
              value={draft.concentration}
              aria-describedby={errors.concentration ? 'condition-concentration-error' : undefined}
              onChange={(event) => setValue('concentration', event.target.value)}
            />
          </Field>
          <Field name="condition-horizon" title="预期持有期限">
            <select
              id="condition-horizon"
              value={draft.horizon}
              onChange={(event) => setValue('horizon', event.target.value)}
            >
              <option value="">尚未选择</option>
              <option>一周以上、一个月以内</option>
              <option>1–3 个月</option>
              <option>3–6 个月</option>
              <option>六个月以上</option>
            </select>
          </Field>
        </div>
        <label className="portfolio-checkbox">
          <input
            type="checkbox"
            checked={draft.noHoldings}
            disabled={hasRecords}
            onChange={(event) => setValue('noHoldings', event.target.checked)}
          />
          我确认当前没有持仓
        </label>
        <small className="muted">
          {hasRecords
            ? '已有基金记录时不能同时声明无持仓；可核对或更正已有记录。'
            : '不勾选且没有记录，表示尚未录入，不代表空仓。'}
        </small>
        <div className="portfolio-form-actions">
          <span className="muted">
            {state.conditions.updatedAt
              ? `上次保存：${displayTime(state.conditions.updatedAt)}`
              : '尚未保存投资条件'}
            {dirty ? ' · 有未保存修改' : ''}
          </span>
          <button className="button primary" type="submit">
            保存投资条件
          </button>
        </div>
      </form>
      <Notice title="建议所用条件">
        已保存的历史决定继续保留其引用的建议版本。修改条件后需在建议页重新评估；演示阶段不生成真实投资方案。
        <Link to="/advice">查看建议与决策 →</Link>
      </Notice>
    </Panel>
  );
}
