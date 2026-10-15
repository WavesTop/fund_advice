import { useMemo, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Download } from 'lucide-react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import { Notice, PageHeader } from '../../shared/ui';
import frozen from './sector-research-snapshot.json';
import frozenDirection from './sector-direction-diagnostic.json';
import {
  candidateRows,
  methodNames,
  methodReasons,
  pct,
  researchMethods,
  researchView,
  scenarioNames,
  stateNames,
  type CandidateMethod,
  type ResearchRow,
  type SectorResearchSnapshot,
} from './sector-research-model';
import './sector-research.css';

const snapshot: SectorResearchSnapshot = frozen;
const direction = { ...frozenDirection, periods: new Map(Object.entries(frozenDirection.periods)) };
const candidateMethods = researchMethods.filter(
  (method): method is CandidateMethod => method !== 'all_industries',
);
function Change({ value }: { value: number }) {
  return <span className={value < 0 ? 'sr-loss' : 'sr-gain'}>{pct(value)}</span>;
}
function Risk({ row }: { row: ResearchRow }) {
  return (
    <>
      {row.recent_fitted_return_pct <= 0 ? '近期拟合方向未向上；' : '近期拟合方向向上；'}
      {row.fast_ma_bias_pct < 0 ? '价格低于短均线；' : '价格高于短均线；'}
      {row.fast_slow_ma_spread_pct < 0 ? '短均线低于长均线。' : '短均线高于长均线。'}
    </>
  );
}
function AccountReference({ separate, children }: { separate: boolean; children: ReactNode }) {
  return separate ? (
    <details>
      <summary>旧模型连续账户参考 · 与价格观察名单不同</summary>
      {children}
    </details>
  ) : (
    children
  );
}
export function SectorResearchTrial() {
  const [params, setParams] = useSearchParams();
  const period =
    snapshot.periods.find((value) => value.id === params.get('horizon')) ?? snapshot.periods[0];
  const view = researchView(params.get('rule'));
  const query = params.get('research_q') ?? '';
  const diagnostic = direction.periods.get(period.id)!;
  const rows = candidateRows(period, view, query, diagnostic.state_codes);
  const emptyRule =
    view === 'price_state'
      ? diagnostic.state_codes.length === 0
      : view !== 'compare' && period.selections[view].length === 0;
  const selected = rows.find((row) => row.code === params.get('sector')) ?? rows[0];
  const update = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(values).forEach(([key, value]) =>
      value ? next.set(key, value) : next.delete(key),
    );
    setParams(next, { replace: true });
  };
  const evidenceMethods =
    view === 'compare' || view === 'price_state'
      ? researchMethods
      : ([view, 'return_top10', 'low_vol_top10', 'all_industries'] as const);
  const columns = [...new Set(evidenceMethods)];
  const option = useMemo<EChartsOption>(
    () => ({
      animation: false,
      color: ['#197565'],
      grid: { left: 56, right: 18, top: 18, bottom: 40 },
      tooltip: { trigger: 'axis', valueFormatter: (value) => `${Number(value).toFixed(2)}%` },
      xAxis: { type: 'category', boundaryGap: false, data: period.dates },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: '{value}%' } },
      series: [{ type: 'line', showSymbol: false, data: selected?.curve ?? [] }],
    }),
    [period, selected],
  );
  const download = () => {
    const url = URL.createObjectURL(
      new Blob(
        [
          JSON.stringify(
            {
              ...snapshot,
              direction_diagnostic: frozenDirection,
              active_view: { horizon: period.id, rule: view },
            },
            null,
            2,
          ),
        ],
        { type: 'application/json' },
      ),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `sector-opportunity-trial-${snapshot.as_of}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="sector-research">
      <PageHeader
        eyebrow="趋势研究 · 冻结试用 V1"
        title="板块机会 · 趋势研究"
        description={`三周期共同出榜日 ${snapshot.as_of} · 同花顺原生行业 ${snapshot.pool_count} 个 · 非实时推荐`}
        actions={
          <button className="button" onClick={download}>
            <Download size={16} />
            导出研究快照
          </button>
        }
      />
      <Notice tone="warning" title="研究候选，尚未达到投资推荐标准">
        长期累计正收益不等于持续盈利。2026 年初新入场的折中试验，三个周期均亏损约 10%～13%。
        历史检验使用行业指数价格和假设费用，不是真实基金收益，也没有未见样本外认证。
      </Notice>
      <div className="sr-provenance">
        <span>候选与价格依据：{snapshot.as_of}</span>
        <span>历史输入截止：{snapshot.data_cutoff}</span>
        <span>完整行业池始于：{snapshot.full_pool_start}</span>
        <strong>冻结快照 · 不随行情自动更新</strong>
      </div>
      <div className="sr-horizons" role="tablist" aria-label="推荐观察周期">
        {snapshot.periods.map((value) => (
          <button
            key={value.id}
            id={`sr-horizon-${value.id}`}
            role="tab"
            aria-selected={value.id === period.id}
            aria-controls="sr-candidate-panel"
            tabIndex={value.id === period.id ? 0 : -1}
            onKeyDown={(event) => {
              const index = snapshot.periods.findIndex((item) => item.id === value.id);
              const nextIndex =
                event.key === 'ArrowRight'
                  ? (index + 1) % 3
                  : event.key === 'ArrowLeft'
                    ? (index + 2) % 3
                    : event.key === 'Home'
                      ? 0
                      : event.key === 'End'
                        ? 2
                        : null;
              if (nextIndex === null) return;
              event.preventDefault();
              const next = snapshot.periods[nextIndex];
              update({ horizon: next.id, sector: '' });
              document.getElementById(`sr-horizon-${next.id}`)?.focus();
            }}
            onClick={() => update({ horizon: value.id, sector: '' })}
          >
            <strong>{value.label}</strong>
            <span>{value.horizon}</span>
            <small>价格稳定上行 {direction.periods.get(value.id)!.state_codes.length} 个</small>
            <small>旧模型联合筛选 {value.selections.upward_quality.length} 个</small>
          </button>
        ))}
      </div>
      <div className="sr-direction-note" role="note" aria-label="价格方向与模型筛选诊断">
        <strong>
          {period.label}价格稳定上行 {diagnostic.state_codes.length} 个，方向五项为正{' '}
          {diagnostic.direction_codes.length} 个。
        </strong>
        <p>
          旧规则价格条件通过 {diagnostic.old_price_gate_count} 个、模型条件通过{' '}
          {diagnostic.old_model_gate_count} 个，两者交集 {diagnostic.old_joint_gate_count}{' '}
          个。模型空名单不等于市场没有上行板块。价格稳定上行仅为当日走势观察，不是未来优势认证。
        </p>
      </div>
      <section
        className="sr-section"
        id="sr-candidate-panel"
        role="tabpanel"
        aria-labelledby={`sr-horizon-${period.id}`}
      >
        <div className="sr-section-heading">
          <div>
            <h2 id="sr-candidates">{period.label}趋势候选 · 优势尚未认证</h2>
            <p>
              {period.observation_start} 至 {period.as_of} · {period.window}{' '}
              个交易日观察窗口；不等于建议持有时长。
            </p>
          </div>
        </div>
        <div className="sr-controls">
          <label>
            研究规则
            <select
              value={view}
              onChange={(event) => update({ rule: event.target.value, sector: '' })}
            >
              <option value="compare">候选对比 · 不作综合排序</option>
              <option value="price_state">价格稳定上行观察 · 未认证</option>
              {candidateMethods.map((method) => (
                <option key={method} value={method}>
                  {methodNames[method]}
                </option>
              ))}
            </select>
          </label>
          <label>
            板块搜索
            <input
              type="search"
              value={query}
              onChange={(event) => update({ research_q: event.target.value })}
            />
          </label>
          <span>
            {rows.length} 个候选 / {snapshot.pool_count} 个同组行业
          </span>
        </div>
        <p className="sr-caption">
          {view === 'compare'
            ? '不同规则的候选并集，按代码排列，无综合名次。'
            : view === 'price_state'
              ? '仅保留已有稳定上行或趋势增强状态，按代码排列，不采用旧模型否决，也不作综合排名。'
              : methodReasons[view]}
          收益分位越高表示预测收益相对靠前，回撤分位越高表示预测回撤相对较小；均非上涨概率或安全保证。搜索不改变原始名次。
        </p>
        <div className="sr-table-scroll" role="region" aria-label="三周期研究候选表" tabIndex={0}>
          <table>
            <thead>
              <tr>
                <th>板块</th>
                <th>观察状态</th>
                <th>模型收益 / 回撤分位</th>
                <th>窗口涨跌</th>
                <th>窗口最大回撤</th>
                <th>入选规则与原始名次</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.code} data-selected={row.code === selected?.code}>
                  <th>
                    <button
                      className="sr-sector-button"
                      aria-pressed={row.code === selected?.code}
                      onClick={() => update({ sector: row.code })}
                      title={`查看${row.name}冻结走势与依据`}
                    >
                      {row.name}
                      <small>{row.code}</small>
                    </button>
                  </th>
                  <td>{stateNames[row.state] ?? row.state}</td>
                  <td>
                    {row.return_percentile.toFixed(1)} / {row.safety_percentile.toFixed(1)}
                  </td>
                  <td>
                    <Change value={row.return_pct} />
                  </td>
                  <td>{Math.abs(row.max_drawdown_pct).toFixed(2)}%</td>
                  <td>
                    {view === 'price_state' && (
                      <span className="sr-membership">价格稳定上行观察 · 未认证</span>
                    )}
                    {candidateMethods
                      .filter((method) => period.selections[method].includes(row.code))
                      .map((method) => (
                        <span className="sr-membership" key={method}>
                          {methodNames[method]} #{period.selections[method].indexOf(row.code) + 1}
                        </span>
                      ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && (
            <div className="sr-empty">
              {emptyRule
                ? view === 'price_state'
                  ? '当日没有板块处于稳定上行或趋势增强状态。数据齐全，不补齐名额；不等于建议卖出或空仓。'
                  : '当日没有板块满足该规则。数据齐全，不补齐名额；不等于建议卖出或空仓。'
                : '没有匹配的板块。'}
            </div>
          )}
        </div>
        {selected && (
          <div className="sr-detail" aria-label="候选趋势依据">
            <div>
              <h3>
                {selected.name} · {stateNames[selected.state]}
              </h3>
              <p>
                <Risk row={selected} />
              </p>
              <dl className="sr-factors">
                <div>
                  <dt>全窗口拟合变化</dt>
                  <dd>
                    <Change value={selected.fitted_return_pct} />
                  </dd>
                </div>
                <div>
                  <dt>近 {selected.recent_sessions} 日拟合变化</dt>
                  <dd>
                    <Change value={selected.recent_fitted_return_pct} />
                  </dd>
                </div>
                <div>
                  <dt>路径拟合度 R²</dt>
                  <dd>{selected.r2.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>上涨交易日占比</dt>
                  <dd>{(selected.up_day_ratio * 100).toFixed(1)}%</dd>
                </div>
                <div>
                  <dt>日波动</dt>
                  <dd>{selected.daily_volatility_pct.toFixed(2)}%</dd>
                </div>
              </dl>
              <p className="sr-caption">
                方向、持续性与回撤是观察依据，不能单独推导未来收益。模型仅使用出榜时已成熟的训练结果，最近标签截止{' '}
                {period.latest_label_end}，共 {period.training_dates} 个训练日期。
              </p>
              <p className="sr-caption">
                转弱观察条件：近期拟合方向转负，或价格与短均线关系由上转下。上行筛选任一条件不满足即不入选，其他规则跌出原始前十即移出对应名单；不是自动交易指令。
              </p>
            </div>
            <div>
              <Chart
                option={option}
                label={`${selected.name}${period.label}冻结价格路径`}
                height={260}
              />
              <p className="sr-caption">行业指数价格变化 · 窗口起点归零 · 非策略净值</p>
            </div>
          </div>
        )}
      </section>
      <section className="sr-section" aria-labelledby="sr-validation">
        <h2 id="sr-validation">历史检验与反例</h2>
        {view === 'price_state' && (
          <>
            <h3>价格上行状态的对应期限检验</h3>
            <p className="sr-caption">
              历史同一状态池取涨幅前十，不足不补齐；评价次日收盘起后续 {period.window}{' '}
              个交易日。下表是有名单批次的价格变化，不是累计账户收益；空名单不填零。当前名单按代码展示，没有历史收益名次。
            </p>
            <div
              className="sr-table-scroll"
              role="region"
              aria-label="价格上行状态历史检验"
              tabIndex={0}
            >
              <table>
                <thead>
                  <tr>
                    <th>排程</th>
                    <th>有名单成熟批次</th>
                    <th>平均后续价格变化</th>
                    <th>相对同数量涨幅差（百分点）</th>
                    <th>最差批次</th>
                  </tr>
                </thead>
                <tbody>
                  {(['primary', 'secondary'] as const).map((clock) => {
                    const result = diagnostic.evidence[clock];
                    return (
                      <tr key={clock}>
                        <th>
                          {clock === 'primary' ? '主排程 · 不重叠评价窗' : '每五交易日 · 重叠描述'}
                        </th>
                        <td>{result.method.matured_batches}</td>
                        <td>
                          <Change value={result.method.mean_selected_return_pct} />
                        </td>
                        <td>{result.vs_count_return.mean_delta_pp.toFixed(2)}</td>
                        <td>
                          <Change value={result.method.worst_batch_return_pct} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="sr-caption">
              有正终点且超过半数未来收盘高于买入价的成员比例，主排程均值{' '}
              {diagnostic.evidence.primary.method.selected_persistent_up_fraction_pct.toFixed(1)}
              %，重叠排程均值{' '}
              {diagnostic.evidence.secondary.method.selected_persistent_up_fraction_pct.toFixed(1)}
              %。这是相关历史样本描述，不是未来胜率。中长期排序和日期稳健性仍未过关。
            </p>
            <p className="sr-direction-caution">
              价格观察名单尚无对应的连续资金回放。以下是原有六组研究账户，仅作参考，不能当作这份价格观察名单的收益。
            </p>
          </>
        )}
        <AccountReference separate={view === 'price_state'}>
          <p>
            {period.evidence.start} 至 {snapshot.data_cutoff} · 初始 100,000 元 · {period.window}{' '}
            个交易日复核 · 单边假设费用 0.10%。
          </p>
          <p className="sr-caption">
            确认 T+1、到账 T+2、普通持有等待 7 个自然日，短赎费下限 1.50%；每个候选目标
            10%，不足名额留现金，含待到账款项与期末赎回费用。均为模型假设，非产品合同。三个周期起点不同，不作跨周期收益排名。
          </p>
          <div className="sr-table-scroll" role="region" aria-label="连续账户历史检验" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th>规则</th>
                  <th>累计收益</th>
                  <th>年化收益</th>
                  <th>最大回撤</th>
                  <th>累计费用（元）</th>
                </tr>
              </thead>
              <tbody>
                {columns.map((method) => {
                  const m = period.evidence.scenarios.fee10_default[method];
                  return (
                    <tr key={method}>
                      <th>{methodNames[method]}</th>
                      <td>
                        <Change value={m.total_return_pct} />
                      </td>
                      <td>
                        <Change value={m.annualized_return_pct} />
                      </td>
                      <td>{Math.abs(m.max_drawdown_pct).toFixed(2)}%</td>
                      <td>{m.total_fees.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <h3>不同日期从现金新入场</h3>
          <p className="sr-caption">
            各行均从该日起独立启动账户，统一截止 {snapshot.data_cutoff}；不是连续账户的区间切片。
          </p>
          <div className="sr-table-scroll" role="region" aria-label="新入场收益检验" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th>实际起点</th>
                  {columns.map((method) => (
                    <th key={method}>{methodNames[method]}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(period.evidence.fresh).map(([day, accounts]) => (
                  <tr key={day}>
                    <th>{accounts[columns[0]].start}</th>
                    {columns.map((method) => (
                      <td key={method}>
                        <Change value={accounts[method].total_return_pct} />
                        <small>
                          回撤 {Math.abs(accounts[method].max_drawdown_pct).toFixed(2)}%
                        </small>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details>
            <summary>牛熊阶段与覆盖缺口</summary>
            <p className="sr-caption">
              预设日期区间，不代表模型识别了牛熊；沿用原账户持仓，不是新入场。2014～2018
              年缺少完整行业池和账户历史，不能据此宣称跨越全部牛熊。
            </p>
            <div className="sr-table-scroll" role="region" aria-label="牛熊阶段检验" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th>预设阶段</th>
                    {columns.map((method) => (
                      <th key={method}>{methodNames[method]}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {period.evidence.episodes.map((episode) => (
                    <tr key={episode.id}>
                      <th>
                        {episode.start} 至 {episode.end}
                        <small>
                          {(
                            {
                              bull: '上涨阶段',
                              bear: '下跌阶段',
                              recovery_observation: '修复观察',
                              up_observation: '上涨观察',
                              down_observation: '下跌观察',
                            } as Record<string, string>
                          )[episode.group] ?? '阶段观察'}
                        </small>
                      </th>
                      {columns.map((method) => {
                        const run = episode.methods[method];
                        return (
                          <td key={method}>
                            {run.value ? (
                              <>
                                <Change value={run.value.total_return_pct} />
                                <small>
                                  回撤 {Math.abs(run.value.max_drawdown_pct).toFixed(2)}%
                                  {run.status === 'partial' ? ' · 部分覆盖' : ''}
                                </small>
                                <small>
                                  {run.value.start_date} 至 {run.value.end_date}
                                </small>
                              </>
                            ) : (
                              '历史不足'
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <details>
            <summary>费用、复核时点与执行敏感性 · 全部 9 组</summary>
            <p className="sr-caption">
              每格为累计收益 / 最大回撤。不同起点和费用会明显改变结果，不以最佳情景代表实际收益。
            </p>
            <div className="sr-table-scroll" role="region" aria-label="敏感性检验" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th>情景</th>
                    {columns.map((method) => (
                      <th key={method}>{methodNames[method]}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(period.evidence.scenarios).map(([id, accounts]) => (
                    <tr key={id}>
                      <th>{scenarioNames[id] ?? id}</th>
                      {columns.map((method) => (
                        <td key={method}>
                          <Change value={accounts[method].total_return_pct} />
                          <small>
                            回撤 {Math.abs(accounts[method].max_drawdown_pct).toFixed(2)}%
                          </small>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </AccountReference>
      </section>
      <footer className="sr-footer">
        <strong>验证边界</strong>
        <p>
          当前行业名单存在存续偏差；没有历史成分可投性、真实基金映射和合同核验。模型收益排序能力较弱，长期折中试验未稳定胜过简单对照。旧模型联合上行筛选当前三周期均无入选，不代表价格稳定上行名单为空。其他规则仍可能选中下行板块，因此只能观察，不能当作已确认的优势推荐。
        </p>
        <details>
          <summary>研究来源与完整性校验</summary>
          {Object.values(snapshot.sources).map((source) => (
            <p key={source.path}>
              {source.path}
              <code>SHA-256 {source.sha256}</code>
            </p>
          ))}
          <p>
            {direction.source.path}
            <code>SHA-256 {direction.source.sha256}</code>
          </p>
        </details>
      </footer>
    </div>
  );
}
