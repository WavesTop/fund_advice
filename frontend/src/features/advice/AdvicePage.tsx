import { useState } from 'react';
import { flushSync } from 'react-dom';
import { Link, useSearchParams } from 'react-router-dom';
import { DATA_DATE, displayTime, funds, getFund, money, periods } from '../../shared/data';
import type { Decision, Period } from '../../shared/types';
import { useDemo } from '../../shared/store';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Tabs } from '../../shared/ui';
import { appendDecision, choices, examples, linkDecision, validateDecision } from './model';
import type { FieldErrors } from '../portfolio/model';
import './advice.css';

type Draft = Omit<Decision, 'id' | 'createdAt'>;
export function AdvicePage() {
  const { state, enqueueTask } = useDemo();
  const [params, setParams] = useSearchParams();
  const tab = ['decisions', 'history'].includes(params.get('tab') || '') ? 'decisions' : 'current';
  const selectedFund = getFund(params.get('fund') || '') ?? funds[0];
  const [evidence, setEvidence] = useState<Period | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [linkTarget, setLinkTarget] = useState<Decision | null>(null);
  const [choiceFilter, setChoiceFilter] = useState('全部决定');
  const [periodFilter, setPeriodFilter] = useState('全部周期');
  const [operationFilter, setOperationFilter] = useState('全部操作');
  const [search, setSearch] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const selectedDecision = state.decisions.find((item) => item.id === params.get('decision'));
  const version = `演示建议 v1 · 策略 ${state.strategyVersion} · ${DATA_DATE}`;
  const missing = [
    !state.conditions.noHoldings && !state.transactions.length ? '持仓范围' : '',
    state.conditions.cash === '' ? '已知现金' : '',
    !state.conditions.risk ? '风险要求' : '',
    !state.conditions.horizon ? '持有期限' : '',
  ].filter(Boolean);
  const linkedTransactions = (id: string) =>
    state.transactions.filter((item) => item.decisionIds.includes(id));
  const showDecision = (id: string) => {
    const next = new URLSearchParams(params);
    next.set('tab', 'decisions');
    next.set('decision', id);
    setParams(next);
  };
  const closeDecision = () => {
    const next = new URLSearchParams(params);
    next.delete('decision');
    setParams(next);
  };
  const startDecision = (period: Period) =>
    setDraft({
      fundCode: selectedFund.code,
      period,
      choice: '',
      amount: '',
      reason: '',
      adviceVersion: version,
      action: examples[period].action,
    });
  const records = [...state.decisions]
    .reverse()
    .filter(
      (item) =>
        (choiceFilter === '全部决定' || item.choice === choiceFilter) &&
        (periodFilter === '全部周期' || item.period === periodFilter) &&
        (operationFilter === '全部操作' ||
          (operationFilter === '尚未操作'
            ? linkedTransactions(item.id).length === 0
            : linkedTransactions(item.id).length > 0)) &&
        (!search ||
          `${getFund(item.fundCode)?.name} ${item.fundCode} ${item.reason}`.includes(search)) &&
        (!params.get('fund') || item.fundCode === selectedFund.code) &&
        (!fromDate || item.createdAt.slice(0, 10) >= fromDate) &&
        (!toDate || item.createdAt.slice(0, 10) <= toDate),
    );

  return (
    <div className="advice-page">
      <PageHeader
        eyebrow="ADVICE & DECISIONS"
        title="建议与决策"
        description="把系统观点、你的选择和实际操作，分别记录清楚。"
        actions={
          <button
            className="button primary"
            onClick={() => enqueueTask(`更新分析演示 · ${selectedFund.code}`)}
          >
            ↻ 更新分析
          </button>
        }
      />
      <Notice title="三周期界面示例 · 未生成真实投资建议">
        以下方向、基金与理由都是虚构展示。尚未执行本地分析或调用
        AI，计划金额不由示例数据推导；三个周期分别比较，实际交易只记一份账。
      </Notice>
      <Tabs
        items={[
          { label: '当前建议', value: 'current' },
          { label: '决策记录', value: 'decisions' },
        ]}
        value={tab}
        onChange={(value) => {
          const next = new URLSearchParams(params);
          next.set('tab', value);
          next.delete('decision');
          setParams(next);
        }}
      />
      {tab === 'current' && (
        <>
          <div className="advice-context">
            <div>
              <span className="muted">展示基金</span>
              <label className="field">
                <span className="sr-only">选择建议基金</span>
                <select
                  aria-label="选择建议基金"
                  value={selectedFund.code}
                  onChange={(event) => {
                    const next = new URLSearchParams(params);
                    next.set('fund', event.target.value);
                    setParams(next);
                  }}
                >
                  {funds.map((item) => (
                    <option key={item.code} value={item.code}>
                      {item.name} · {item.code} · {item.share}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div>
              <span className="muted">示例资料日期</span>
              <strong>{DATA_DATE}</strong>
            </div>
            <div>
              <span className="muted">策略展示版本</span>
              <strong>{state.strategyVersion}</strong>
            </div>
            <div>
              <span className="muted">分析参与情况</span>
              <strong>本地计算 / AI 均未执行</strong>
            </div>
          </div>
          {missing.length > 0 ? (
            <Notice tone="warning" title="个性化条件尚不完整">
              待补充：{missing.join('、')}。目前只能查看相关基金的展示示例，不能生成个性化金额。{' '}
              <Link to={`/portfolio?tab=conditions&fund=${selectedFund.code}`}>完善投资条件 →</Link>
            </Notice>
          ) : (
            <Notice title="投资条件已录入">
              条件保存于{' '}
              {state.conditions.updatedAt
                ? displayTime(state.conditions.updatedAt)
                : '尚未记录时间'}
              ；分析服务接入后才可生成个性化方案。当前卡片仍是演示数据。
            </Notice>
          )}
          {state.scenario === 'stale' && (
            <Notice tone="warning" title="示例依据已过期">
              卡片保留旧版展示，不能作为当前判断。记录决定时会保留所引用的演示版本。
            </Notice>
          )}
          {state.scenario === 'ai-conflict' && (
            <Notice tone="warning" title="本地与 AI 意见冲突 · 场景演示">
              本地示例：暂不操作，等待数据核对。AI
              示例：可关注后续变化，但市场陈述缺乏来源支持、待核实。两者未合并，未形成可模拟操作；实际
              AI 未调用。
            </Notice>
          )}
          <div className="period-grid advice-periods">
            {periods.map((period) => (
              <Panel
                key={period.id}
                className={`advice-period advice-${period.id}`}
                title={
                  <>
                    <span className="advice-period-dot" style={{ background: period.color }} />
                    {period.name}
                  </>
                }
                subtitle={period.range}
                action={<Badge tone="neutral">独立方案</Badge>}
              >
                {state.scenario === 'empty' || state.scenario === 'loading' ? (
                  <EmptyState
                    title={state.scenario === 'loading' ? '演示分析加载中' : '该周期资料不足'}
                    description={
                      state.scenario === 'loading'
                        ? '保留周期位置，完成状态由后台任务提供。'
                        : '当前没有可展示的分析，不能将缺失资料表示为没有机会。'
                    }
                    action={
                      <Link className="button secondary" to="/settings?tab=data">
                        查看资料与任务
                      </Link>
                    }
                  />
                ) : (
                  <>
                    <div className="advice-direction">
                      <span className="muted">板块方向 · 示例</span>
                      <h3>{examples[period.id].sector}</h3>
                      <Badge tone="amber">尚未个性化</Badge>
                    </div>
                    <div className="advice-candidate">
                      <span className="muted">相关基金</span>
                      <Link to={`/funds/${selectedFund.code}`}>{selectedFund.name}</Link>
                      <small>
                        {selectedFund.code} · {selectedFund.share}
                      </small>
                    </div>
                    <div className="advice-action">
                      <span className="muted">持仓操作展示</span>
                      <strong>{examples[period.id].action}</strong>
                      <span>计划金额：待计算</span>
                    </div>
                    <p className="advice-reason">{examples[period.id].reason}</p>
                    <div className="advice-risk">
                      <strong>主要风险</strong>
                      <p>{examples[period.id].risk}</p>
                    </div>
                    <p className="advice-condition">
                      <span className="muted">适用前提</span>
                      <br />
                      {examples[period.id].condition}
                    </p>
                    <small className="muted">依据日期 {DATA_DATE} · 仅界面示例</small>
                    <div className="advice-card-actions">
                      <button className="button secondary" onClick={() => setEvidence(period.id)}>
                        查看依据
                      </button>
                      <button className="button primary" onClick={() => startDecision(period.id)}>
                        记录决定
                      </button>
                    </div>
                  </>
                )}
              </Panel>
            ))}
          </div>
          <Panel title="从观点到实际操作" subtitle="“采纳”只保存选择，不自动改变持仓或生成交易。">
            <ol className="advice-flow">
              <li>
                <span>01</span>
                <div>
                  <strong>比较三个周期</strong>
                  <p>保留不同方向及各自的适用条件。</p>
                </div>
              </li>
              <li>
                <span>02</span>
                <div>
                  <strong>记录个人决定</strong>
                  <p>说明采纳程度、调整计划与个人理由。</p>
                </div>
              </li>
              <li>
                <span>03</span>
                <div>
                  <strong>单独记录实际操作</strong>
                  <p>交易发生后填写确认信息，再建立关联。</p>
                </div>
              </li>
            </ol>
          </Panel>
        </>
      )}
      {tab === 'decisions' && (
        <Panel
          title="个人决定历史"
          subtitle="决定类型和实际操作状态分开展示；新增决定保留旧记录。"
          action={<Badge tone="neutral">{state.decisions.length} 条本地演示记录</Badge>}
        >
          <div className="toolbar advice-filters">
            <label className="field">
              搜索基金 / 理由
              <input
                aria-label="搜索决策记录"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="名称、代码或个人理由"
              />
            </label>
            <label className="field">
              决定类型
              <select
                value={choiceFilter}
                onChange={(event) => setChoiceFilter(event.target.value)}
              >
                <option>全部决定</option>
                {choices.map((choice) => (
                  <option key={choice}>{choice}</option>
                ))}
              </select>
            </label>
            <label className="field">
              周期
              <select
                value={periodFilter}
                onChange={(event) => setPeriodFilter(event.target.value)}
              >
                <option>全部周期</option>
                {periods.map((period) => (
                  <option key={period.id} value={period.id}>
                    {period.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              实际操作
              <select
                value={operationFilter}
                onChange={(event) => setOperationFilter(event.target.value)}
              >
                {['全部操作', '尚未操作', '已关联操作'].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="field">
              记录起日
              <input
                aria-label="决定记录起日"
                type="date"
                value={fromDate}
                onChange={(event) => setFromDate(event.target.value)}
              />
            </label>
            <label className="field">
              记录止日
              <input
                aria-label="决定记录止日"
                type="date"
                value={toDate}
                onChange={(event) => setToDate(event.target.value)}
              />
            </label>
          </div>
          {params.get('fund') && (
            <p className="muted">
              限定基金 {selectedFund.code} ·{' '}
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
            </p>
          )}
          {records.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>决定时间 / 周期</th>
                    <th>基金 / 系统示例</th>
                    <th>个人选择</th>
                    <th>实际操作状态</th>
                    <th>回看</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((item) => {
                    const related = linkedTransactions(item.id);
                    const superseded = new Set(
                      state.transactions.map((transaction) => transaction.corrects).filter(Boolean),
                    );
                    const effective = related.filter(
                      (transaction) =>
                        transaction.status !== '已撤销' && !superseded.has(transaction.id),
                    );
                    return (
                      <tr key={item.id}>
                        <td>
                          {displayTime(item.createdAt)}
                          <small>
                            {periods.find((period) => period.id === item.period)?.name} · 当时记录
                          </small>
                        </td>
                        <td>
                          {getFund(item.fundCode)?.name}
                          <small>
                            {item.fundCode} · {item.action}
                          </small>
                        </td>
                        <td>
                          <Badge tone={item.choice === '未采纳' ? 'neutral' : 'blue'}>
                            {item.choice}
                          </Badge>
                          <small>
                            {item.amount ? `计划 ¥ ${money(item.amount)}` : '未填写交易计划金额'}
                          </small>
                        </td>
                        <td>
                          {effective.length
                            ? `已关联 ${effective.length} 笔操作`
                            : related.length
                              ? '关联记录已更正 / 撤销'
                              : '尚未操作'}
                          <small>关联不代表已成交</small>
                        </td>
                        <td>
                          <button
                            className="button secondary"
                            onClick={() => showDecision(item.id)}
                          >
                            查看决定
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="还没有符合条件的个人决定"
              description="在任一周期记录选择，之后可以关联一笔或多笔实际操作。"
              action={
                <Link className="button primary" to="/advice">
                  查看三个周期
                </Link>
              }
            />
          )}
        </Panel>
      )}

      {evidence && (
        <Modal
          title={`${periods.find((period) => period.id === evidence)?.name}建议依据 · 界面示例`}
          onClose={() => setEvidence(null)}
          wide
        >
          <Badge tone="amber">虚构展示 · 不构成有效建议</Badge>
          <h3>
            {selectedFund.name} · {examples[evidence].action}
          </h3>
          <p>{examples[evidence].reason}</p>
          <Notice tone="warning" title="主要风险">
            {examples[evidence].risk}
          </Notice>
          <div className="grid-two">
            <Panel title="来源事实">
              <p>此演示使用固定虚构基金资料，日期 {DATA_DATE}。</p>
              <p className="muted">没有接入真实行情或披露来源，不能视为最新市场事实。</p>
              <Link to={`/funds/${selectedFund.code}`}>查看基金资料展示 →</Link>
            </Panel>
            <Panel title="本地计算与量化结果">
              <p>尚未执行分析，评分、目标金额及费用均待计算。</p>
              <Badge tone="neutral">等待业务模块</Badge>
            </Panel>
            <Panel title="AI 分析">
              <p>
                {state.scenario === 'ai-conflict'
                  ? '示例 AI 认为可以关注，与本地“暂不操作”冲突；陈述来源待核实，综合状态为暂缓形成操作。'
                  : 'AI 尚未调用。配置后由后续分析模块提供观点、引用与校验状态。'}
              </p>
              <Badge tone="amber">暂不能用于操作模拟</Badge>
            </Panel>
            <Panel title="交易与资金约束">
              <p>
                {missing.length
                  ? `待补充：${missing.join('、')}`
                  : '投资条件已填写，尚未进行业务校验。'}
              </p>
              <p>可用份额、费用规则和资金占用未核对。</p>
              <Link to={`/portfolio?tab=conditions&fund=${selectedFund.code}`}>查看投资条件 →</Link>
            </Panel>
          </div>
          <details className="advice-evidence-more">
            <summary>查看版本与所用输入</summary>
            <p>{version}</p>
            <p>
              该展示未读取个人条件生成结论；真实分析尚未接入。历史决定保存所引用的示例版本与操作摘要。
            </p>
          </details>
          <div className="advice-card-actions">
            <button
              className="button primary"
              onClick={() => {
                startDecision(evidence);
                setEvidence(null);
              }}
            >
              记录对此示例的决定
            </button>
          </div>
        </Modal>
      )}
      {draft && (
        <DecisionEditor
          initial={draft}
          onClose={() => setDraft(null)}
          onSaved={(id) => {
            // Remove the saved form's guard before navigating to its new record.
            flushSync(() => setDraft(null));
            showDecision(id);
          }}
        />
      )}
      {selectedDecision && !draft && !linkTarget && (
        <Modal title="个人决定 · 当时记录" onClose={closeDecision} wide>
          <div className="advice-decision-header">
            <Badge tone="blue">{selectedDecision.choice}</Badge>
            <span>{displayTime(selectedDecision.createdAt)}</span>
            <Badge tone="neutral">演示记录</Badge>
          </div>
          <h3>
            {getFund(selectedDecision.fundCode)?.name} · {selectedDecision.fundCode}
          </h3>
          <dl className="advice-detail-list">
            <dt>引用的建议版本</dt>
            <dd>{selectedDecision.adviceVersion}</dd>
            <dt>当时周期 / 操作</dt>
            <dd>
              {periods.find((period) => period.id === selectedDecision.period)?.name} /{' '}
              {selectedDecision.action}
            </dd>
            <dt>个人计划金额</dt>
            <dd>{selectedDecision.amount ? `¥ ${money(selectedDecision.amount)}` : '未填写'}</dd>
            <dt>个人理由 / 观察条件</dt>
            <dd>{selectedDecision.reason || '未填写'}</dd>
          </dl>
          <Notice>
            这份决定不会修改实际持仓。所引用的是固定演示建议；后续的新数据或新策略不会替换此处保存的版本和选择。
          </Notice>
          <h3>关联操作时间线</h3>
          <ol className="advice-timeline">
            <li>
              <strong>参考示例版本</strong>
              <p>{selectedDecision.adviceVersion} · 无真实分析生成事件</p>
            </li>
            <li>
              <strong>记录个人决定</strong>
              <p>
                {displayTime(selectedDecision.createdAt)} · {selectedDecision.choice}
              </p>
            </li>
            {linkedTransactions(selectedDecision.id).map((item) => (
              <li key={item.id}>
                <strong>
                  {item.kind} · {item.status}
                  {item.corrects ? ' · 更正 / 撤销' : ''}
                </strong>
                <p>
                  实际发生 {item.date} · 记录于 {displayTime(item.createdAt)}
                </p>
                <p>
                  {item.amount ? `¥ ${money(item.amount)}` : '金额待补全'} · 确认{' '}
                  {item.confirmationDate || '待确认'} · 到账 {item.settlementDate || '待补全'}
                </p>
                <Link to={`/portfolio?tab=transactions&fund=${item.fundCode}`}>查看关联交易 →</Link>
              </li>
            ))}
          </ol>
          {linkedTransactions(selectedDecision.id).length === 0 && (
            <p className="muted">尚未关联实际操作。计划、决定和实际交易是不同记录。</p>
          )}
          <div className="advice-card-actions">
            <Link
              className="button primary"
              to={`/portfolio?tab=transactions&action=trade&fund=${selectedDecision.fundCode}&decision=${selectedDecision.id}`}
            >
              记录实际操作
            </Link>
            <button className="button secondary" onClick={() => setLinkTarget(selectedDecision)}>
              关联已有交易
            </button>
            <button
              className="button secondary"
              onClick={() =>
                setDraft({
                  ...selectedDecision,
                  reason: `变更自决定 ${selectedDecision.id}；原选择：${selectedDecision.choice}。\n`,
                  choice: '',
                })
              }
            >
              追加决定变更
            </button>
            <Link
              className="button secondary"
              to={`/review?tab=timeline&decision=${selectedDecision.id}`}
            >
              进入复盘 →
            </Link>
          </div>
        </Modal>
      )}
      {linkTarget && <LinkTransactions decision={linkTarget} onClose={() => setLinkTarget(null)} />}
    </div>
  );
}

function DecisionEditor({
  initial,
  onClose,
  onSaved,
}: {
  initial: Draft;
  onClose: () => void;
  onSaved: (id: string) => void;
}) {
  const { update, notify } = useDemo();
  const [draft, setDraft] = useState(initial);
  const [errors, setErrors] = useState<FieldErrors>({});
  return (
    <Modal
      title="记录个人决定"
      onClose={onClose}
      wide
      dirty={JSON.stringify(draft) !== JSON.stringify(initial)}
    >
      <p className="muted">{draft.adviceVersion}</p>
      <h3>
        {getFund(draft.fundCode)?.name} · {periods.find((item) => item.id === draft.period)?.name}
      </h3>
      <p>系统操作示例：{draft.action} · 金额待计算</p>
      <Notice>
        保存决定只会追加个人选择记录，不创建交易、不改变持仓。当前引用内容为演示，实际操作需要单独确认。
      </Notice>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          const validation = validateDecision(draft);
          setErrors(validation);
          if (Object.keys(validation).length) return;
          const id = crypto.randomUUID();
          const saved = update((previous) =>
            appendDecision(previous, { ...draft, id, createdAt: new Date().toISOString() }),
          );
          if (saved) {
            notify('个人决定已保存，实际持仓未改变。');
            onSaved(id);
          }
        }}
      >
        <fieldset className="advice-choice-group">
          <legend>我的选择</legend>
          {choices.map((choice) => (
            <label className={draft.choice === choice ? 'selected' : ''} key={choice}>
              <input
                type="radio"
                name="decision-choice"
                value={choice}
                checked={draft.choice === choice}
                onChange={() => {
                  setDraft((previous) => ({ ...previous, choice }));
                  setErrors({});
                }}
              />
              <span>{choice}</span>
              <small>
                {choice === '全部采纳'
                  ? '认可这个周期的操作方向'
                  : choice === '部分采纳'
                    ? '保留部分观点，调整计划'
                    : choice === '未采纳'
                      ? '记录自己的判断与计划'
                      : '保留观察条件，暂不行动'}
              </small>
            </label>
          ))}
        </fieldset>
        {errors.choice && (
          <p role="alert" className="portfolio-error">
            {errors.choice}
          </p>
        )}
        <label className="field" htmlFor="decision-amount">
          {draft.choice === '部分采纳'
            ? '调整后的计划金额（元，必填）'
            : '自己的计划金额（元，可留空）'}
        </label>
        <input
          id="decision-amount"
          className="advice-full-input"
          inputMode="decimal"
          aria-describedby={errors.amount ? 'decision-amount-error' : undefined}
          placeholder="计划不是实际成交金额"
          value={draft.amount}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, amount: event.target.value }))
          }
        />
        {errors.amount && (
          <p id="decision-amount-error" role="alert" className="portfolio-error">
            {errors.amount}
          </p>
        )}
        <label className="field advice-reason-field" htmlFor="decision-reason">
          {draft.choice === '暂不操作'
            ? '观察条件 / 暂不操作的理由'
            : draft.choice === '部分采纳'
              ? '调整理由与具体计划（必填）'
              : '自己的理由与计划'}
        </label>
        <textarea
          id="decision-reason"
          className="advice-full-input"
          aria-describedby={errors.reason ? 'decision-reason-error' : undefined}
          rows={4}
          placeholder="说明你如何考虑这条观点，以及之后准备观察什么"
          value={draft.reason}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, reason: event.target.value }))
          }
        />
        {errors.reason && (
          <p id="decision-reason-error" role="alert" className="portfolio-error">
            {errors.reason}
          </p>
        )}
        <div className="portfolio-form-actions">
          <button type="submit" className="button primary">
            保存个人决定
          </button>
        </div>
      </form>
    </Modal>
  );
}

function LinkTransactions({ decision, onClose }: { decision: Decision; onClose: () => void }) {
  const { state, update, notify } = useDemo();
  const [selected, setSelected] = useState<string[]>([]);
  const superseded = new Set(state.transactions.map((item) => item.corrects).filter(Boolean));
  const available = state.transactions.filter(
    (item) =>
      item.fundCode === decision.fundCode &&
      item.status !== '已撤销' &&
      !superseded.has(item.id) &&
      !item.decisionIds.includes(decision.id),
  );
  return (
    <Modal title="关联已有交易" onClose={onClose} wide dirty={selected.length > 0}>
      <Notice>
        关联只用于回看，同一笔交易可参考多个决定，但仍只保存一笔交易。仅展示同基金且尚未关联的有效记录。
      </Notice>
      {available.length ? (
        <>
          <div className="advice-link-list">
            {available.map((item) => (
              <label key={item.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(item.id)}
                  onChange={(event) =>
                    setSelected((previous) =>
                      event.target.checked
                        ? [...previous, item.id]
                        : previous.filter((id) => id !== item.id),
                    )
                  }
                />
                <div>
                  <strong>
                    {item.date} · {item.kind}
                  </strong>
                  <p>
                    {item.amount ? `¥ ${money(item.amount)}` : '金额待补全'} ·{' '}
                    {item.shares || '未填'} 份 · {item.channel}
                  </p>
                </div>
                <Badge tone="neutral">{item.status}</Badge>
              </label>
            ))}
          </div>
          <div className="portfolio-form-actions">
            <button
              className="button primary"
              disabled={!selected.length}
              onClick={() => {
                const saved = update((previous) => linkDecision(previous, decision.id, selected));
                if (saved) {
                  notify(`已关联 ${selected.length} 笔已有交易，没有新增或重复记账。`);
                  onClose();
                }
              }}
            >
              确认关联 {selected.length} 笔
            </button>
          </div>
        </>
      ) : (
        <EmptyState
          title="暂无可以关联的已有交易"
          description="同基金的未撤销记录可在这里关联，已关联的条目不会重复显示。"
          action={
            <Link
              className="button primary"
              to={`/portfolio?tab=transactions&action=trade&fund=${decision.fundCode}&decision=${decision.id}`}
            >
              记录实际操作
            </Link>
          }
        />
      )}
    </Modal>
  );
}
