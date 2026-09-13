import { useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowDownToLine, ArrowUpFromLine, Database, RefreshCw, ShieldCheck } from 'lucide-react';
import { DATA_DATE, displayTime } from '../../shared/data';
import { useDemo } from '../../shared/store';
import { Badge, EmptyState, Modal, Notice, PageHeader, Panel, Tabs } from '../../shared/ui';
import type { DemoTask, Scenario } from '../../shared/types';
import { createDemoExport, MAX_IMPORT_BYTES, parseDemoImport, scenarios } from './importDemo';
import './settings.css';

const tabs = [
  { label: '数据与更新', value: 'data' },
  { label: '任务记录', value: 'tasks' },
  { label: 'AI 分析', value: 'ai' },
  { label: '本地资料与运行', value: 'local' },
];
const dataGroups = [
  {
    name: '基金目录',
    coverage: '8 个虚构基金身份',
    date: DATA_DATE,
    missing: '全市场目录待接入',
    type: '基础资料',
  },
  {
    name: '行情 / 净值',
    coverage: '预设走势与净值样例',
    date: DATA_DATE,
    missing: '真实连续历史待接入',
    type: '市场数据',
  },
  {
    name: '披露持仓',
    coverage: '部分持仓 / 联接穿透样例',
    date: '2026-06-30',
    missing: '未披露部分保留未知',
    type: '公开披露',
  },
  {
    name: '板块分类',
    coverage: '行业与主题示例分类',
    date: DATA_DATE,
    missing: '分类依据与版本待接入',
    type: '分类资料',
  },
  {
    name: '交易规则',
    coverage: '费用展示与录入入口',
    date: '—',
    missing: '渠道规则尚未确认',
    type: '交易资料',
  },
  {
    name: '分析资料',
    coverage: '三周期分析展示样例',
    date: DATA_DATE,
    missing: '真实分析与引用待接入',
    type: '分析输入',
  },
];

export function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const tab = tabs.some((item) => item.value === params.get('tab')) ? params.get('tab')! : 'data';
  return (
    <div className="settings-page">
      <PageHeader
        eyebrow="PREFERENCES & DATA"
        title="设置"
        description="了解资料从哪里来、任务进行到哪一步，以及本机保存了什么。"
        actions={<Badge tone="neutral">本机使用 · 界面演示</Badge>}
      />
      <Tabs items={tabs} value={tab} onChange={(value) => setParams({ tab: value })} />
      {tab === 'data' && <DataSettings />}
      {tab === 'tasks' && <TaskSettings />}
      {tab === 'ai' && <AISettings />}
      {tab === 'local' && <LocalSettings />}
      <ScenarioSettings />
    </div>
  );
}

function DataSettings() {
  const { state, enqueueTask } = useDemo();
  const [detail, setDetail] = useState<(typeof dataGroups)[number] | null>(null);
  const [backfill, setBackfill] = useState(false);
  const [object, setObject] = useState('行情 / 净值');
  const [from, setFrom] = useState('2026-03-11');
  const [to, setTo] = useState(DATA_DATE);
  const [error, setError] = useState('');
  const queueBackfill = (event: FormEvent) => {
    event.preventDefault();
    if (!from || !to || from > to || to > DATA_DATE) {
      setError(`请选择有效的起止日期，截止日期不得晚于示例资料日 ${DATA_DATE}。`);
      return;
    }
    if (enqueueTask(`历史回填 · ${object} · ${from} 至 ${to}`)) setBackfill(false);
  };
  return (
    <>
      <div className="settings-status-banner">
        <span className="settings-status-icon">
          <Database size={24} />
        </span>
        <div>
          <h2>资料状态一目了然</h2>
          <p>目前使用预设演示资料。实际采集、数据校验与后台更新将在后续阶段接入。</p>
        </div>
        <button className="button primary" onClick={() => enqueueTask('更新全部演示资料')}>
          <RefreshCw size={14} /> 更新全部资料
        </button>
      </div>
      <Panel
        title="资料覆盖"
        subtitle="来源均为应用内虚构样例；日期表示样例所反映的日期，不表示已完成网络获取。"
        action={
          <button
            className="button secondary"
            onClick={() => {
              setBackfill(true);
              setError('');
            }}
          >
            历史回填
          </button>
        }
      >
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>资料类别</th>
                <th>可查看范围</th>
                <th>资料日期</th>
                <th>缺口与限制</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {dataGroups.map((group) => (
                <tr key={group.name}>
                  <td>
                    <b>{group.name}</b>
                    <small className="settings-table-meta">{group.type}</small>
                  </td>
                  <td>{state.scenario === 'empty' ? '尚无资料 · 场景演示' : group.coverage}</td>
                  <td>{group.date}</td>
                  <td>
                    <span className="settings-missing">{group.missing}</span>
                  </td>
                  <td>
                    <div className="toolbar">
                      <button
                        className="settings-text-button"
                        onClick={() => enqueueTask(`更新 ${group.name}`)}
                      >
                        更新
                      </button>
                      <button className="settings-text-button" onClick={() => setDetail(group)}>
                        详情
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <div className="grid-two">
        <Panel title="更新与恢复" subtitle="离开页面后，可从任务记录继续查看演示队列。">
          <ul className="settings-explanation">
            <li>同名且仍在排队的任务会定位已有任务，不重复入队。</li>
            <li>当前没有真实执行进度，不显示虚构百分比或完成时间。</li>
            <li>未来断网或休眠恢复后，需要重新查询后台状态，再补跑缺失资料。</li>
          </ul>
          <Link to="/settings?tab=tasks">查看任务记录 →</Link>
        </Panel>
        <Panel title="资料日期的含义" subtitle="披露、获取与分析生成是不同的时间。">
          <div className="settings-key-values">
            <span>数据反映日期</span>
            <b>表中各资料日期</b>
            <span>公开 / 披露时间</span>
            <b>待真实来源提供</b>
            <span>真实获取时间</span>
            <b>尚未获取</b>
            <span>最近成功更新</span>
            <b>尚无真实更新记录</b>
          </div>
        </Panel>
      </div>
      {detail && (
        <Modal title={`${detail.name} · 来源与覆盖`} onClose={() => setDetail(null)}>
          <div className="settings-key-values">
            <span>来源</span>
            <b>应用内预设虚构样例</b>
            <span>已展示范围</span>
            <b>{detail.coverage}</b>
            <span>资料日期</span>
            <b>{detail.date}</b>
            <span>缺失内容</span>
            <b>{detail.missing}</b>
            <span>原始外部资料</span>
            <b>尚未接入</b>
            <span>最近更新结果</span>
            <b>未发生实际更新</b>
          </div>
          <p className="muted">
            加入队列仅保存一个演示任务，不会让本资料变为真实可用，也不会自动生成分析。
          </p>
          <div className="toolbar settings-modal-actions">
            <button className="button secondary" onClick={() => setDetail(null)}>
              关闭
            </button>
            <button
              className="button primary"
              onClick={() => {
                if (enqueueTask(`更新 ${detail.name}`)) setDetail(null);
              }}
            >
              补全资料入队
            </button>
          </div>
        </Modal>
      )}
      {backfill && (
        <Modal title="历史回填 · 演示任务" onClose={() => setBackfill(false)}>
          <form onSubmit={queueBackfill}>
            <Notice tone="info">
              仅保存回填请求样例，不访问外部服务。未来回填只补资料或明确标记的回算，不补造当时发布的建议。
            </Notice>
            <label className="field">
              <span>资料类别</span>
              <select value={object} onChange={(event) => setObject(event.target.value)}>
                {dataGroups.map((group) => (
                  <option key={group.name}>{group.name}</option>
                ))}
              </select>
            </label>
            <div className="grid-two">
              <label className="field">
                <span>开始日期</span>
                <input
                  type="date"
                  value={from}
                  max={to || DATA_DATE}
                  onChange={(event) => setFrom(event.target.value)}
                  required
                />
              </label>
              <label className="field">
                <span>截止日期</span>
                <input
                  type="date"
                  value={to}
                  min={from}
                  max={DATA_DATE}
                  onChange={(event) => setTo(event.target.value)}
                  required
                />
              </label>
            </div>
            {error && (
              <p className="field-error" role="alert">
                {error}
              </p>
            )}
            <div className="toolbar settings-modal-actions">
              <button type="button" className="button secondary" onClick={() => setBackfill(false)}>
                取消
              </button>
              <button type="submit" className="button primary">
                加入演示队列
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

function TaskSettings() {
  const { state, update, enqueueTask, notify } = useDemo();
  const [filter, setFilter] = useState('全部');
  const [detail, setDetail] = useState<string | null>(null);
  const [cancel, setCancel] = useState<string | null>(null);
  const tasks = [...state.tasks]
    .reverse()
    .filter((task) => filter === '全部' || task.status === filter);
  const selected = state.tasks.find((task) => task.id === detail);
  const cancelling = state.tasks.find((task) => task.id === cancel);
  const tone = (task: DemoTask) =>
    task.status === '失败'
      ? ('red' as const)
      : task.status === '排队中'
        ? ('blue' as const)
        : ('neutral' as const);
  return (
    <>
      <Notice title="演示队列 · 后台执行尚未接入" tone="info">
        任务不会自动采集、分析或显示成功。排队、取消、失败与重试操作用于验证界面与记录保留；实际恢复状态必须由后续后台服务提供。
      </Notice>
      <div className="settings-task-stats">
        <div>
          <span>演示排队</span>
          <b>{state.tasks.filter((task) => task.status === '排队中').length}</b>
        </div>
        <div>
          <span>演示失败</span>
          <b>{state.tasks.filter((task) => task.status === '失败').length}</b>
        </div>
        <div>
          <span>已取消</span>
          <b>{state.tasks.filter((task) => task.status === '已取消').length}</b>
        </div>
        <div>
          <span>实际后台状态</span>
          <b className="settings-task-service">待接入</b>
        </div>
      </div>
      <Panel
        title="任务记录"
        subtitle="重复提交不会创建第二个排队任务。失败后保留原记录，重试另建一次尝试。"
        action={
          <select
            aria-label="筛选任务状态"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            {['全部', '排队中', '失败', '已取消', '演示完成'].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        }
      >
        {!tasks.length ? (
          <EmptyState
            title="没有符合条件的任务"
            description="从资料更新、代码补录或更新分析入口加入一个演示任务，即可查看其状态。"
            action={
              <Link to="/settings?tab=data" className="button secondary">
                前往数据与更新
              </Link>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>任务对象</th>
                  <th>状态 / 阶段</th>
                  <th>发起时间 · 北京时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <b>{task.name}</b>
                      <small className="settings-table-meta">
                        演示记录 · {task.id.slice(0, 8)}
                      </small>
                    </td>
                    <td>
                      <Badge tone={tone(task)}>{task.status}</Badge>
                      <small className="settings-table-meta">
                        {task.status === '排队中'
                          ? '等待实际后台接入'
                          : task.status === '失败'
                            ? '示例失败，未产生真实结果'
                            : task.status === '已取消'
                              ? '已移出演示等待队列'
                              : '导入的演示标记，非真实成功'}
                      </small>
                    </td>
                    <td>{displayTime(task.createdAt)}</td>
                    <td>
                      <div className="toolbar">
                        <button className="settings-text-button" onClick={() => setDetail(task.id)}>
                          查看详情
                        </button>
                        {task.status === '排队中' && (
                          <button
                            className="settings-text-button"
                            onClick={() => setCancel(task.id)}
                          >
                            取消
                          </button>
                        )}
                        {(task.status === '失败' || task.status === '已取消') && (
                          <button
                            className="settings-text-button"
                            onClick={() => enqueueTask(task.name)}
                          >
                            重试
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {selected && (
        <Modal title="任务详情" onClose={() => setDetail(null)}>
          <div className="settings-key-values">
            <span>任务</span>
            <b>{selected.name}</b>
            <span>状态</span>
            <b>{selected.status}</b>
            <span>记录时间</span>
            <b>{displayTime(selected.createdAt)} · 北京时间</b>
            <span>说明</span>
            <b>{selected.detail}</b>
            <span>已保存实际结果</span>
            <b>无 · 当前没有执行真实任务</b>
            <span>任务编号</span>
            <b className="settings-break">{selected.id}</b>
          </div>
          {selected.status === '排队中' && (
            <div className="settings-test-action">
              <p className="muted">
                验收工具：把此演示任务标为失败，检查失败说明与重试流程。不是实际后台失败。
              </p>
              <button
                className="button secondary"
                onClick={() => {
                  if (
                    update((previous) => ({
                      ...previous,
                      tasks: previous.tasks.map((task) =>
                        task.id === selected.id
                          ? {
                              ...task,
                              status: '失败',
                              detail:
                                '验收场景：示例来源连接超时。未下载资料，未生成分析；可重试加入演示队列。',
                            }
                          : task,
                      ),
                    }))
                  )
                    notify('已切换为任务失败的演示状态。');
                }}
              >
                演示任务失败
              </button>
            </div>
          )}
          <div className="toolbar settings-modal-actions">
            <button className="button primary" onClick={() => setDetail(null)}>
              完成查看
            </button>
          </div>
        </Modal>
      )}
      {cancelling && (
        <Modal title="取消演示任务" onClose={() => setCancel(null)}>
          <p>
            将取消「{cancelling.name}
            」。此任务尚未实际运行，没有需要回滚的采集结果；任务记录会保留。
          </p>
          <div className="toolbar settings-modal-actions">
            <button className="button secondary" onClick={() => setCancel(null)}>
              继续等待
            </button>
            <button
              className="button primary"
              onClick={() => {
                if (
                  update((previous) => ({
                    ...previous,
                    tasks: previous.tasks.map((task) =>
                      task.id === cancelling.id
                        ? {
                            ...task,
                            status: '已取消',
                            detail: '本人取消了演示队列中的请求，未执行实际后台任务。',
                          }
                        : task,
                    ),
                  }))
                ) {
                  notify('演示任务已取消，记录已保留。');
                  setCancel(null);
                }
              }}
            >
              确认取消任务
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function AISettings() {
  const { state } = useDemo();
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [portfolio, setPortfolio] = useState(false);
  const [conditions, setConditions] = useState(false);
  const [preview, setPreview] = useState(false);
  const [error, setError] = useState('');
  const previewConfig = (event: FormEvent) => {
    event.preventDefault();
    if (!provider || !model.trim() || !endpoint.trim()) {
      setError('请填写服务类型、模型名称与接口地址，再预览资料发送范围。');
      return;
    }
    try {
      const url = new URL(endpoint);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
        throw new Error();
    } catch {
      setError('接口地址应为 HTTP / HTTPS 地址，且不能包含账号或密钥。');
      return;
    }
    setError('');
    setPreview(true);
  };
  return (
    <>
      <div className="settings-status-banner">
        <span className="settings-status-icon">
          <ShieldCheck size={25} />
        </span>
        <div>
          <h2>AI 为可选能力</h2>
          <p>未启用时，基金浏览与已有本地资料仍可使用。当前没有任何外部调用。</p>
        </div>
        <Badge tone="neutral">未启用 · 待后端接入</Badge>
      </div>
      <div className="grid-two">
        <Panel
          title="配置与发送范围预览"
          subtitle="以下输入仅在当前页面内预览，不会保存密钥、连接外部服务或启用 AI。"
        >
          <form onSubmit={previewConfig}>
            <label className="field">
              <span>服务类型</span>
              <select value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="">请选择待接入的服务类型</option>
                <option value="兼容接口">兼容接口（接入预览）</option>
                <option value="本地模型服务">本地模型服务（接入预览）</option>
              </select>
            </label>
            <label className="field">
              <span>模型名称</span>
              <input
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="填写计划使用的模型名称"
                maxLength={100}
                autoComplete="off"
              />
            </label>
            <label className="field">
              <span>接口地址 · 不含密钥</span>
              <input
                type="url"
                value={endpoint}
                onChange={(event) => setEndpoint(event.target.value)}
                placeholder="https://example.com/v1"
                maxLength={500}
                autoComplete="off"
              />
            </label>
            <label className="field">
              <span>API 密钥</span>
              <input
                type="password"
                disabled
                value=""
                placeholder="安全保存能力待后端接入，当前不接收密钥"
                autoComplete="off"
              />
            </label>
            <fieldset className="settings-scope">
              <legend>计划发送的数据类别</legend>
              <label>
                <input type="checkbox" checked disabled /> 公开资料与对应来源
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={portfolio}
                  onChange={(event) => setPortfolio(event.target.checked)}
                />{' '}
                个人持仓与交易摘要
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={conditions}
                  onChange={(event) => setConditions(event.target.checked)}
                />{' '}
                投资条件与风险要求
              </label>
            </fieldset>
            {error && (
              <p className="field-error" role="alert">
                {error}
              </p>
            )}
            <div className="toolbar">
              <button className="button primary" type="submit">
                预览发送范围
              </button>
              <button
                className="button secondary"
                type="button"
                disabled
                title="连接检查需后端安全配置与实际调用能力"
              >
                检查连接 · 待接入
              </button>
            </div>
            <p className="muted settings-fineprint">
              配置预览不表示服务已支持。正式启用及范围变化时，将先展示接收方与发送内容，由本人确认。
            </p>
          </form>
        </Panel>
        <div className="settings-stack">
          <Panel title="使用情况" subtitle="没有真实调用，不按文字质量推断 AI 的有效性。">
            <div className="settings-key-values">
              <span>启用状态</span>
              <b>未启用</b>
              <span>调用记录</span>
              <b>尚无调用</b>
              <span>费用 / 用量</span>
              <b>— · 尚无服务端数据</b>
              <span>旧 AI 输出</span>
              <b>无真实历史输出</b>
            </div>
          </Panel>
          <Panel title="缺失、失败与冲突" subtitle="这些状态分别说明受影响范围。">
            <div className="settings-ai-state">
              <Badge tone={state.scenario === 'ai-conflict' ? 'amber' : 'neutral'}>
                {state.scenario === 'ai-conflict' ? '判断冲突 · 演示' : '配置尚未启用'}
              </Badge>
              <p>
                {state.scenario === 'ai-conflict'
                  ? '示例本地规则提示先观察；示例 AI 观点偏积极，但缺少独立来源支持。两种判断分别保留，未合并成可执行结论。'
                  : '当前没有 AI 结果。配置缺失、超时、限流或输出校验失败不会替换已有的本地资料。'}
              </p>
              <Link to="/advice">查看建议中的依据与状态 →</Link>
            </div>
            <details className="settings-disclosure">
              <summary>查看正式接入后的状态说明</summary>
              <ul>
                <li>连接失败 / 超时：保留原输入，本次结果缺失。</li>
                <li>限流：说明来源与可重试条件，不虚报执行成功。</li>
                <li>输出校验失败：原始输出可追溯，不作为有效结论。</li>
                <li>资料不足：列出缺口，无来源支持的市场陈述标为待核实。</li>
                <li>旧结果：保留原时间、模型与提示版本，不冒充新结果。</li>
              </ul>
            </details>
          </Panel>
        </div>
      </div>
      {preview && (
        <Modal title="资料发送范围 · 仅预览" onClose={() => setPreview(false)}>
          <Notice tone="info">
            这是配置预览。关闭或继续浏览均不会发送数据，也不会保存这些设置。
          </Notice>
          <div className="settings-key-values">
            <span>接收服务类型</span>
            <b>{provider}</b>
            <span>接口</span>
            <b className="settings-break">{endpoint}</b>
            <span>模型</span>
            <b>{model}</b>
            <span>用途</span>
            <b>辅助解释已有资料与投资条件</b>
            <span>公开资料</span>
            <b>包含来源与时点</b>
            <span>个人持仓 / 交易</span>
            <b>{portfolio ? '计划包含，正式启用前需确认' : '不包含'}</b>
            <span>投资条件</span>
            <b>{conditions ? '计划包含，正式启用前需确认' : '不包含'}</b>
            <span>实际发送</span>
            <b>未发送</b>
          </div>
          <button className="button primary" onClick={() => setPreview(false)}>
            完成预览
          </button>
        </Modal>
      )}
    </>
  );
}

function LocalSettings() {
  const { state, update, notify, resetDemo } = useDemo();
  const [pending, setPending] = useState<ReturnType<typeof parseDemoImport> | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState('');
  const [reset, setReset] = useState(false);
  const [runtime, setRuntime] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const exportRecords = () => {
    const blob = new Blob([createDemoExport(state)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `fund-advice-demo-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify('已生成演示记录 JSON 下载；文件仅包含当前演示草稿，不是成套数据库备份。');
  };
  const importRecords = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setError('');
    try {
      if (file.size > MAX_IMPORT_BYTES) throw new Error('文件超过 2 MB，未导入。');
      setPending(parseDemoImport(await file.text()));
      setAcknowledged(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法读取文件，当前记录保持不变。');
    }
    event.target.value = '';
  };
  return (
    <>
      <Notice title="当前保存的是浏览器内的演示草稿" tone="info">
        交易、决定、复盘笔记、费用规则与版本采用记录保存在本机浏览器中，用于验证界面。它们不是实际账本；数据库与原始资料的成套备份、恢复和真实运行状态仍待后端接入。
      </Notice>
      <div className="grid-two">
        <Panel
          title="演示记录导出 / 导入"
          subtitle="JSON 仅包含当前浏览器中的演示记录；不包含密钥、真实行情或原始披露文件。"
        >
          <div className="settings-record-counts">
            <div>
              <strong>{state.transactions.length}</strong>
              <span>交易草稿</span>
            </div>
            <div>
              <strong>{state.decisions.length}</strong>
              <span>个人决定</span>
            </div>
            <div>
              <strong>{state.strategyHistory.length}</strong>
              <span>版本采用</span>
            </div>
          </div>
          <div className="toolbar">
            <button className="button primary" onClick={exportRecords}>
              <ArrowDownToLine size={15} /> 导出演示记录
            </button>
            <button className="button secondary" onClick={() => input.current?.click()}>
              <ArrowUpFromLine size={15} /> 导入演示记录
            </button>
            <input
              className="settings-file-input"
              ref={input}
              type="file"
              accept="application/json,.json"
              aria-label="选择演示记录 JSON 文件"
              onChange={importRecords}
            />
          </div>
          <p className="muted settings-fineprint">
            导入前验证格式、字段、历史关联及版本，再展示替换摘要。仅支持本应用导出的第 1
            版演示记录，文件不超过 2 MB。
          </p>
          {error && (
            <Notice tone="error" title="导入未执行">
              {error} 当前已保存记录保持不变。
            </Notice>
          )}
        </Panel>
        <Panel title="完整备份" subtitle="未来将成套包含数据库、原始资料及必要配置。">
          <div className="settings-key-values">
            <span>完整资料位置</span>
            <b>待本地服务提供</b>
            <span>最近完整备份</span>
            <b>尚未执行</b>
            <span>备份状态</span>
            <b>待后端接入</b>
          </div>
          <div className="toolbar">
            <button className="button secondary" disabled title="成套数据库备份尚未接入">
              创建完整备份 · 待接入
            </button>
            <button className="button secondary" disabled title="数据库与原始资料恢复尚未接入">
              恢复完整备份 · 待接入
            </button>
          </div>
          <p className="muted settings-fineprint">上方的演示 JSON 导出不等同于这里的完整备份。</p>
        </Panel>
      </div>
      <Panel
        title="运行状态"
        subtitle="此处只报告当前确实可知的前端状态。"
        action={
          <button className="button secondary" onClick={() => setRuntime(true)}>
            查看运行详情
          </button>
        }
      >
        <div className="settings-runtime-grid">
          <div>
            <span className="settings-runtime-dot" />
            <b>浏览器界面</b>
            <span>已加载</span>
          </div>
          <div>
            <span className="settings-runtime-dot neutral" />
            <b>本地数据服务</b>
            <span>尚未接入</span>
          </div>
          <div>
            <span className="settings-runtime-dot neutral" />
            <b>后台分析与更新</b>
            <span>尚未接入</span>
          </div>
        </div>
      </Panel>
      <Panel
        title="重置演示"
        subtitle="清空本应用的演示草稿与队列，回到初始界面。建议先导出以便恢复。"
        action={
          <button className="button secondary" onClick={() => setReset(true)}>
            重置演示记录
          </button>
        }
      >
        <p className="muted settings-fineprint">
          不影响其他应用或浏览器资料；已下载的 JSON 文件可重新导入。
        </p>
      </Panel>
      {pending && (
        <Modal title="确认导入演示记录" onClose={() => setPending(null)}>
          <Notice tone="warning">
            将替换当前演示记录。请先导出当前内容，以便需要时恢复。此操作不涉及实际数据库。
          </Notice>
          <div className="settings-key-values">
            <span>文件导出时间</span>
            <b>{displayTime(pending.exportedAt)} · 北京时间</b>
            <span>交易草稿</span>
            <b>
              {state.transactions.length} → {pending.state.transactions.length} 笔
            </b>
            <span>个人决定</span>
            <b>
              {state.decisions.length} → {pending.state.decisions.length} 条
            </b>
            <span>演示任务</span>
            <b>
              {state.tasks.length} → {pending.state.tasks.length} 条
            </b>
            <span>版本采用记录</span>
            <b>
              {state.strategyHistory.length} → {pending.state.strategyHistory.length} 条
            </b>
            <span>当前策略版本</span>
            <b>
              {state.strategyVersion} → {pending.state.strategyVersion}
            </b>
            <span>复盘笔记与条件</span>
            <b>一并替换</b>
            <span>导入后的场景</span>
            <b>{scenarios.find((item) => item.value === pending.state.scenario)?.label}</b>
          </div>
          <label className="settings-check">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />{' '}
            我已核对范围，并理解将替换当前演示记录
          </label>
          <div className="toolbar settings-modal-actions">
            <button className="button secondary" onClick={exportRecords}>
              先导出当前记录
            </button>
            <button
              className="button primary"
              disabled={!acknowledged}
              onClick={() => {
                if (update(() => pending.state)) {
                  notify('演示记录已导入，未恢复或改变实际数据库。');
                  setPending(null);
                }
              }}
            >
              确认导入
            </button>
          </div>
        </Modal>
      )}
      {reset && (
        <Modal title="确认重置演示记录" onClose={() => setReset(false)}>
          <p>
            将移除当前演示中的 {state.transactions.length} 笔交易、{state.decisions.length} 个决定、
            {state.tasks.length} 个任务，以及投资条件、费用规则、复盘笔记和策略采用历史。
          </p>
          <p className="muted">重置后可从此前导出的演示 JSON 恢复；未导出的内容不能自动找回。</p>
          <div className="toolbar settings-modal-actions">
            <button className="button secondary" onClick={exportRecords}>
              先导出记录
            </button>
            <button className="button secondary" onClick={() => setReset(false)}>
              保留记录
            </button>
            <button
              className="button primary"
              onClick={() => {
                resetDemo();
                setReset(false);
              }}
            >
              确认重置演示
            </button>
          </div>
        </Modal>
      )}
      {runtime && (
        <Modal title="本机运行详情" onClose={() => setRuntime(false)}>
          <div className="settings-key-values">
            <span>当前应用</span>
            <b>3.1 界面展示与交互</b>
            <span>保存位置</span>
            <b>当前网站的浏览器 localStorage</b>
            <span>账本 / SQLite</span>
            <b>尚未接入，未写入实际账本</b>
            <span>网络更新</span>
            <b>未执行</b>
            <span>后台分析</span>
            <b>未执行</b>
            <span>真实数据占用</span>
            <b>— · 待本地服务提供</b>
            <span>当前验收场景</span>
            <b>{scenarios.find((item) => item.value === state.scenario)?.label}</b>
          </div>
          <p className="muted">
            浏览器草稿不能作为真实任务成功的凭据。后续接入本地服务后，状态与恢复结果均需查询服务确认。
          </p>
          <button className="button primary" onClick={() => setRuntime(false)}>
            完成查看
          </button>
        </Modal>
      )}
    </>
  );
}

function ScenarioSettings() {
  const { state, update, notify } = useDemo();
  const selected = scenarios.find((item) => item.value === state.scenario)!;
  return (
    <Panel
      className="settings-scenario-panel"
      title="界面验收场景"
      subtitle="切换展示与交互异常，便于检查状态表达；切换为空记录不会删除已保存的草稿。"
      action={
        <select
          aria-label="界面验收场景"
          value={state.scenario}
          onChange={(event) => {
            const scenario = event.target.value as Scenario;
            if (update((previous) => ({ ...previous, scenario })))
              notify(
                `已切换为「${scenarios.find((item) => item.value === scenario)?.label}」演示场景。`,
              );
          }}
        >
          {scenarios.map((item) => (
            <option value={item.value} key={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      }
    >
      <p className="muted settings-fineprint">
        {selected.description} 场景设定不代表实际网络、后台或 AI 的运行结果。
      </p>
    </Panel>
  );
}
