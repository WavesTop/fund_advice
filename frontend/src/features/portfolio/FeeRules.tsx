import { useState } from 'react';
import { funds, getFund } from '../../shared/data';
import type { FeeRule } from '../../shared/types';
import { useDemo } from '../../shared/store';
import { Badge, EmptyState, Modal, Notice, Panel } from '../../shared/ui';
import { emptyTier, tierLabel, validateFeeRule, type FeeTier } from './fee-model';
import type { FieldErrors } from './model';

export function FeeRules({ fundCode }: { fundCode: string }) {
  const { state } = useDemo();
  const [editing, setEditing] = useState<FeeRule | null>(null);
  const rules = state.feeRules.filter((rule) => !fundCode || rule.fundCode === fundCode);
  return (
    <>
      <Notice title="费用规则单独核对" tone="warning">
        适用基金、份额类别、渠道及生效日期必须一致。录入值只用于演示，不自动应用到交易，也不替代实际确认费用。
      </Notice>
      <Panel
        title="赎回费规则"
        subtitle="按持有期限逐行管理区间；未知费率保持空白。"
        action={
          <button
            className="button primary"
            onClick={() =>
              setEditing({
                id: crypto.randomUUID(),
                fundCode,
                channel: '',
                source: '',
                effectiveDate: '',
                confirmed: false,
                tiers: [emptyTier()],
              })
            }
          >
            ＋ 录入交易规则
          </button>
        }
      >
        {rules.length ? (
          <div className="portfolio-fee-rules">
            {rules.map((rule) => {
              const validation = validateFeeRule(rule);
              return (
                <article key={rule.id}>
                  <div className="portfolio-context">
                    <div>
                      <strong>
                        {getFund(rule.fundCode)?.name} · {rule.fundCode} ·{' '}
                        {getFund(rule.fundCode)?.share}
                      </strong>
                      <p>
                        {rule.channel} · 生效日期 {rule.effectiveDate || '待补全'}
                      </p>
                    </div>
                    <Badge tone={rule.confirmed ? 'blue' : 'amber'}>
                      {rule.confirmed ? '本人标记已核对 · 演示' : '规则未核对'}
                    </Badge>
                  </div>
                  <p className="muted">来源：{rule.source || '待补全'} · 实际费用试算：尚未接入</p>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>持有期限（边界明确）</th>
                          <th>赎回费率</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rule.tiers.map((tier, index) => (
                          <tr key={index}>
                            <td>{tierLabel(tier)}</td>
                            <td>
                              {tier.rate === '' ? (
                                <Badge tone="amber">未知</Badge>
                              ) : (
                                `${tier.rate}%`
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {validation.gaps.length > 0 && (
                    <p className="portfolio-error">覆盖缺口：{validation.gaps.join('；')}</p>
                  )}
                  <button
                    className="button secondary"
                    onClick={() => setEditing(structuredClone(rule))}
                  >
                    编辑演示规则
                  </button>
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyState
            title="尚未录入任何交易规则"
            description="请参考对应渠道的费率说明填写，不会自动填入示例费率。"
          />
        )}
      </Panel>
      {editing && <FeeRuleEditor initial={editing} onClose={() => setEditing(null)} />}
    </>
  );
}

function FeeRuleEditor({ initial, onClose }: { initial: FeeRule; onClose: () => void }) {
  const { update, notify } = useDemo();
  const [draft, setDraft] = useState(initial);
  const [errors, setErrors] = useState<FieldErrors>({});
  const validation = validateFeeRule(draft);
  const modifyTier = (index: number, key: keyof FeeTier, value: string | boolean) =>
    setDraft((previous) => ({
      ...previous,
      tiers: previous.tiers.map((tier, current) =>
        current === index ? { ...tier, [key]: value } : tier,
      ),
    }));
  return (
    <Modal
      title="录入交易规则 · 演示"
      onClose={onClose}
      wide
      dirty={JSON.stringify(draft) !== JSON.stringify(initial)}
    >
      <Notice>
        本金与费用计算尚未接入。本页按整数持有天数检查区间；实际持有日数起算及计算口径以后续业务规则为准。
      </Notice>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          const result = validateFeeRule(draft);
          setErrors(result.errors);
          if (Object.keys(result.errors).length) return;
          const saved = update((previous) => ({
            ...previous,
            feeRules: previous.feeRules.some((rule) => rule.id === draft.id)
              ? previous.feeRules.map((rule) => (rule.id === draft.id ? draft : rule))
              : [...previous.feeRules, draft],
          }));
          if (saved) {
            notify('演示费率规则已保存，尚未应用于任何费用核算。');
            onClose();
          }
        }}
      >
        <div className="grid-two">
          <label className="field">
            基金与份额类别
            <select
              aria-label="费率适用基金"
              value={draft.fundCode}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, fundCode: event.target.value }))
              }
            >
              <option value="">请选择</option>
              {funds.map((fund) => (
                <option value={fund.code} key={fund.code}>
                  {fund.name} · {fund.code} · {fund.share}
                </option>
              ))}
            </select>
            {errors.fundCode && (
              <small role="alert" className="portfolio-error">
                {errors.fundCode}
              </small>
            )}
          </label>
          <label className="field">
            适用渠道
            <input
              aria-label="费率适用渠道"
              placeholder="例如：实际销售平台 / 账户"
              value={draft.channel}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, channel: event.target.value }))
              }
            />
            {errors.channel && (
              <small role="alert" className="portfolio-error">
                {errors.channel}
              </small>
            )}
          </label>
          <label className="field">
            规则来源
            <input
              aria-label="费率规则来源"
              placeholder="费率说明名称、地址或确认材料"
              value={draft.source}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, source: event.target.value }))
              }
            />
            {errors.source && (
              <small role="alert" className="portfolio-error">
                {errors.source}
              </small>
            )}
          </label>
          <label className="field">
            生效日期
            <input
              type="date"
              aria-label="费率生效日期"
              value={draft.effectiveDate}
              onChange={(event) =>
                setDraft((previous) => ({ ...previous, effectiveDate: event.target.value }))
              }
            />
            {errors.effectiveDate && (
              <small role="alert" className="portfolio-error">
                {errors.effectiveDate}
              </small>
            )}
          </label>
        </div>
        <h3>持有期限区间</h3>
        <p className="muted">上界留空表示无上限。下界、上界与是否包含边界分别填写。</p>
        <div className="portfolio-tiers">
          {draft.tiers.map((tier, index) => (
            <fieldset key={index}>
              <legend>第 {index + 1} 个区间</legend>
              <div className="grid-two">
                <label className="field">
                  下界（天）
                  <input
                    inputMode="numeric"
                    aria-label={`区间${index + 1}下界`}
                    value={tier.minDays}
                    onChange={(event) => modifyTier(index, 'minDays', event.target.value)}
                  />
                </label>
                <label className="field">
                  上界（天，可留空）
                  <input
                    inputMode="numeric"
                    aria-label={`区间${index + 1}上界`}
                    value={tier.maxDays}
                    onChange={(event) => modifyTier(index, 'maxDays', event.target.value)}
                  />
                </label>
              </div>
              <div className="portfolio-tier-boundaries">
                <label className="portfolio-checkbox">
                  <input
                    type="checkbox"
                    checked={tier.minInclusive}
                    onChange={(event) => modifyTier(index, 'minInclusive', event.target.checked)}
                  />
                  包含下界
                </label>
                <label className="portfolio-checkbox">
                  <input
                    type="checkbox"
                    checked={tier.maxInclusive}
                    onChange={(event) => modifyTier(index, 'maxInclusive', event.target.checked)}
                    disabled={!tier.maxDays}
                  />
                  包含上界
                </label>
              </div>
              <label className="field">
                费率（%，未知留空）
                <input
                  inputMode="decimal"
                  aria-label={`区间${index + 1}费率`}
                  placeholder="未填入示例费率"
                  value={tier.rate}
                  onChange={(event) => modifyTier(index, 'rate', event.target.value)}
                />
              </label>
              {tier.minDays !== '' && <p className="muted">范围预览：{tierLabel(tier)}</p>}
              {errors[`tier-${index}`] && (
                <p className="portfolio-error" role="alert">
                  {errors[`tier-${index}`]}
                </p>
              )}
              <button
                type="button"
                className="button secondary"
                disabled={draft.tiers.length === 1}
                onClick={() =>
                  setDraft((previous) => ({
                    ...previous,
                    tiers: previous.tiers.filter((_, current) => current !== index),
                  }))
                }
              >
                移除此未保存区间
              </button>
            </fieldset>
          ))}
        </div>
        <button
          type="button"
          className="button secondary"
          onClick={() =>
            setDraft((previous) => ({ ...previous, tiers: [...previous.tiers, emptyTier()] }))
          }
        >
          ＋ 添加持有期限区间
        </button>
        {validation.gaps.length > 0 && (
          <Notice tone="warning" title="期限覆盖存在缺口">
            {validation.gaps.join('；')}。可先保存为未核对规则，后续继续补全。
          </Notice>
        )}
        <label className="portfolio-checkbox">
          <input
            type="checkbox"
            checked={draft.confirmed}
            onChange={(event) =>
              setDraft((previous) => ({ ...previous, confirmed: event.target.checked }))
            }
          />
          我已核对来源、生效范围及全部费率（演示确认）
        </label>
        {errors.coverage && (
          <p className="portfolio-error" role="alert">
            {errors.coverage}
          </p>
        )}
        <div className="portfolio-form-actions">
          <button type="submit" className="button primary">
            保存演示规则
          </button>
        </div>
      </form>
    </Modal>
  );
}
