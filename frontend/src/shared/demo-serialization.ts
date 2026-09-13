import type {
  Conditions,
  Decision,
  DemoState,
  DemoTask,
  FeeRule,
  Scenario,
  Transaction,
} from './types';

export const DEMO_EXPORT_KIND = 'fund-advice-ui-demo';
export const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
export const scenarios: { value: Scenario; label: string; description: string }[] = [
  { value: 'ready', label: '正常展示', description: '查看完整的虚构资料与交互。' },
  { value: 'empty', label: '首次使用 / 空记录', description: '展示空白引导，不清除已保存草稿。' },
  { value: 'loading', label: '加载中', description: '演示等待资料的状态，不虚报进度。' },
  { value: 'offline', label: '联网失败', description: '显示断网说明，仍可查看已有演示资料。' },
  {
    value: 'error',
    label: '保存失败',
    description: '新的保存会失败，已有内容保留；可随时切回正常。',
  },
  { value: 'stale', label: '资料过期', description: '显示原日期与更新提示。' },
  {
    value: 'ai-conflict',
    label: 'AI 与本地判断冲突',
    description: '分别展示两种示例判断及证据缺口。',
  },
];

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error(`${label}格式不正确。`);
  return value as Record<string, unknown>;
}
function onlyKeys(value: Record<string, unknown>, keys: string[], label: string) {
  if (Object.keys(value).some((key) => !keys.includes(key)))
    throw new Error(`${label}包含不支持的字段，未导入。`);
}
function text(value: unknown, label: string, limit = 1000, required = false): string {
  if (typeof value !== 'string' || value.length > limit || (required && !value.trim()))
    throw new Error(`${label}必须是${required ? '非空' : ''}文本，且不超过 ${limit} 字。`);
  return value;
}
function timestamp(value: unknown, label: string, optional = false): string {
  const result = text(value, label, 40, !optional);
  if (optional && result === '') return result;
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(result) ||
    !Number.isFinite(Date.parse(result)) ||
    new Date(result.slice(0, 10)).toISOString().slice(0, 10) !== result.slice(0, 10)
  )
    throw new Error(`${label}不是有效时间。`);
  return result;
}
function date(value: unknown, label: string): string {
  const result = text(value, label, 10);
  if (
    result &&
    (!/^\d{4}-\d{2}-\d{2}$/.test(result) ||
      !Number.isFinite(Date.parse(result)) ||
      new Date(result).toISOString().slice(0, 10) !== result)
  )
    throw new Error(`${label}不是有效日期。`);
  return result;
}
function numeric(value: unknown, label: string): string {
  const result = text(value, label, 30);
  if (result && !/^\d{1,18}(?:\.\d{1,8})?$/.test(result))
    throw new Error(`${label}必须是非负十进制数。`);
  return result;
}
function choice<T extends string>(value: unknown, allowed: readonly T[], label: string): T {
  if (typeof value !== 'string' || !allowed.includes(value as T))
    throw new Error(`${label}不受支持。`);
  return value as T;
}
function list<T>(value: unknown, label: string, parse: (value: unknown) => T): T[] {
  if (!Array.isArray(value) || value.length > 10000)
    throw new Error(`${label}必须是列表，且最多包含 10,000 条。`);
  return value.map(parse);
}
function uniqueIds(items: { id: string }[], label: string) {
  if (new Set(items.map((item) => item.id)).size !== items.length)
    throw new Error(`${label}包含重复编号。`);
}
function fundCode(value: unknown, label: string, allowEmpty = false) {
  const result = text(value, label, 6, !allowEmpty);
  if (!(allowEmpty && result === '') && !/^\d{6}$/.test(result))
    throw new Error(`${label}应为六位基金代码。`);
  return result;
}

function parseTransaction(value: unknown): Transaction {
  const item = object(value, '交易记录');
  onlyKeys(
    item,
    [
      'id',
      'fundCode',
      'kind',
      'amount',
      'shares',
      'date',
      'confirmationDate',
      'settlementDate',
      'channel',
      'status',
      'fee',
      'note',
      'decisionIds',
      'createdAt',
      'corrects',
    ],
    '交易记录',
  );
  return {
    id: text(item.id, '交易编号', 100, true),
    fundCode: fundCode(item.fundCode, '交易基金代码', true),
    kind: text(item.kind, '交易类型', 80, true),
    amount: numeric(item.amount, '交易金额'),
    shares: numeric(item.shares, '交易份额'),
    date: date(item.date, '交易日期'),
    confirmationDate: date(item.confirmationDate, '确认日期'),
    settlementDate: date(item.settlementDate, '到账日期'),
    channel: text(item.channel, '交易渠道', 200),
    status: choice(item.status, ['待补全', '待确认', '已确认', '待核对', '已撤销'], '交易状态'),
    fee: numeric(item.fee, '交易费用'),
    note: text(item.note, '交易备注', 10000),
    decisionIds: list(item.decisionIds, '关联决定', (id) => text(id, '关联决定编号', 100, true)),
    createdAt: timestamp(item.createdAt, '交易记录时间'),
    ...(item.corrects === undefined
      ? {}
      : { corrects: text(item.corrects, '原交易编号', 100, true) }),
  };
}
function parseDecision(value: unknown): Decision {
  const item = object(value, '决定记录');
  onlyKeys(
    item,
    [
      'id',
      'fundCode',
      'period',
      'choice',
      'amount',
      'reason',
      'createdAt',
      'adviceVersion',
      'action',
    ],
    '决定记录',
  );
  return {
    id: text(item.id, '决定编号', 100, true),
    fundCode: fundCode(item.fundCode, '决定基金代码'),
    period: choice(item.period, ['short', 'medium', 'long'], '决定周期'),
    choice: text(item.choice, '个人选择', 100, true),
    amount: numeric(item.amount, '决定金额'),
    reason: text(item.reason, '决定理由', 10000),
    createdAt: timestamp(item.createdAt, '决定记录时间'),
    adviceVersion: text(item.adviceVersion, '建议版本', 120, true),
    action: text(item.action, '计划操作', 200, true),
  };
}
function parseTask(value: unknown): DemoTask {
  const item = object(value, '任务记录');
  onlyKeys(item, ['id', 'name', 'status', 'createdAt', 'detail'], '任务记录');
  return {
    id: text(item.id, '任务编号', 100, true),
    name: text(item.name, '任务名称', 200, true),
    status: choice(item.status, ['排队中', '演示完成', '失败', '已取消'], '任务状态'),
    createdAt: timestamp(item.createdAt, '任务时间'),
    detail: text(item.detail, '任务说明', 10000),
  };
}
function parseConditions(value: unknown): Conditions {
  const item = object(value, '投资条件');
  onlyKeys(
    item,
    ['cash', 'additional', 'risk', 'concentration', 'horizon', 'noHoldings', 'updatedAt'],
    '投资条件',
  );
  if (typeof item.noHoldings !== 'boolean') throw new Error('无持仓确认必须是布尔值。');
  return {
    cash: numeric(item.cash, '现金'),
    additional: numeric(item.additional, '追加资金'),
    risk: text(item.risk, '风险条件', 200),
    concentration: text(item.concentration, '集中度条件', 200),
    horizon: text(item.horizon, '持有期限', 200),
    noHoldings: item.noHoldings,
    updatedAt: timestamp(item.updatedAt, '条件更新时间', true),
  };
}

function parseFeeRule(value: unknown): FeeRule {
  const item = object(value, '费用规则');
  onlyKeys(
    item,
    ['id', 'fundCode', 'channel', 'source', 'effectiveDate', 'confirmed', 'tiers'],
    '费用规则',
  );
  if (typeof item.confirmed !== 'boolean') throw new Error('费用规则确认状态必须是布尔值。');
  const tiers = list(item.tiers, '费率区间', (value) => {
    const tier = object(value, '费率区间');
    onlyKeys(tier, ['minDays', 'maxDays', 'minInclusive', 'maxInclusive', 'rate'], '费率区间');
    if (typeof tier.minInclusive !== 'boolean' || typeof tier.maxInclusive !== 'boolean')
      throw new Error('费率边界是否包含必须是布尔值。');
    const minDays = numeric(tier.minDays, '最小持有天数');
    const maxDays = numeric(tier.maxDays, '最大持有天数');
    const rate = numeric(tier.rate, '费率');
    if ((minDays && !/^\d+$/.test(minDays)) || (maxDays && !/^\d+$/.test(maxDays)))
      throw new Error('持有天数必须是整数。');
    if (rate && Number(rate) > 100) throw new Error('费率不能超过 100%。');
    if (minDays && maxDays && Number(minDays) > Number(maxDays))
      throw new Error('费率区间的起止天数无效。');
    return {
      minDays,
      maxDays,
      minInclusive: tier.minInclusive,
      maxInclusive: tier.maxInclusive,
      rate,
    };
  });
  return {
    id: text(item.id, '费用规则编号', 100, true),
    fundCode: fundCode(item.fundCode, '费用规则基金代码'),
    channel: text(item.channel, '费用适用渠道', 200),
    source: text(item.source, '费率来源', 1000),
    effectiveDate: date(item.effectiveDate, '费率生效日期'),
    confirmed: item.confirmed,
    tiers,
  };
}

export function parseDemoState(value: unknown): DemoState {
  const state = object(value, '演示记录');
  onlyKeys(
    state,
    [
      'scenario',
      'transactions',
      'decisions',
      'conditions',
      'tasks',
      'strategyVersion',
      'strategyHistory',
      'reviewNotes',
      'feeRules',
    ],
    '演示记录',
  );
  const transactions = list(state.transactions, '交易记录', parseTransaction);
  const decisions = list(state.decisions, '决定记录', parseDecision);
  const tasks = list(state.tasks, '任务记录', parseTask);
  const feeRules = list(state.feeRules ?? [], '费用规则', parseFeeRule);
  const strategyHistory = list(state.strategyHistory, '版本采用记录', (value) => {
    const item = object(value, '版本采用记录');
    onlyKeys(item, ['id', 'version', 'at', 'reason'], '版本采用记录');
    return {
      id: text(item.id, '采用编号', 100, true),
      version: choice(item.version, ['v1.0', 'v1.1'], '采用版本'),
      at: timestamp(item.at, '采用时间'),
      reason: text(item.reason, '采用理由', 10000, true),
    };
  });
  uniqueIds(transactions, '交易记录');
  uniqueIds(decisions, '决定记录');
  uniqueIds(tasks, '任务记录');
  uniqueIds(strategyHistory, '版本采用记录');
  uniqueIds(feeRules, '费用规则');
  const decisionIds = new Set(decisions.map((item) => item.id));
  const transactionIds = new Set(transactions.map((item) => item.id));
  for (const transaction of transactions) {
    if (new Set(transaction.decisionIds).size !== transaction.decisionIds.length)
      throw new Error('同一交易重复关联了同一个决定。');
    if (transaction.decisionIds.some((id) => !decisionIds.has(id)))
      throw new Error('交易关联的决定不存在，不能导入不完整的历史。');
    if (
      transaction.corrects &&
      (transaction.corrects === transaction.id || !transactionIds.has(transaction.corrects))
    )
      throw new Error('交易更正链引用无效。');
  }
  const correctedIds = transactions.flatMap((transaction) =>
    transaction.corrects ? [transaction.corrects] : [],
  );
  if (new Set(correctedIds).size !== correctedIds.length)
    throw new Error('交易更正链出现分叉，同一记录不能被直接更正两次。');
  const previousById = new Map(
    transactions.map((transaction) => [transaction.id, transaction.corrects]),
  );
  for (const transaction of transactions) {
    const visited = new Set<string>();
    let cursor: string | undefined = transaction.id;
    while (cursor) {
      if (visited.has(cursor)) throw new Error('交易更正链出现循环。');
      visited.add(cursor);
      cursor = previousById.get(cursor);
    }
  }
  const strategyVersion = choice(state.strategyVersion, ['v1.0', 'v1.1'], '当前策略版本');
  if (
    (strategyHistory.length ? strategyHistory[strategyHistory.length - 1].version : 'v1.0') !==
    strategyVersion
  )
    throw new Error('当前策略版本与采用历史不一致。');
  return {
    scenario: choice(
      state.scenario,
      scenarios.map((item) => item.value),
      '演示场景',
    ),
    transactions,
    decisions,
    tasks,
    feeRules,
    conditions: parseConditions(state.conditions),
    strategyVersion,
    strategyHistory,
    reviewNotes: text(state.reviewNotes, '复盘笔记', 10000),
  };
}

export function createDemoExport(state: DemoState, exportedAt = new Date().toISOString()): string {
  return JSON.stringify({ kind: DEMO_EXPORT_KIND, schemaVersion: 1, exportedAt, state }, null, 2);
}

export function parseDemoImport(raw: string): { state: DemoState; exportedAt: string } {
  if (new TextEncoder().encode(raw).byteLength > MAX_IMPORT_BYTES)
    throw new Error('文件超过 2 MB，未导入。');
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('文件不是有效 JSON，当前演示记录保持不变。');
  }
  const envelope = object(parsed, '导入文件');
  onlyKeys(envelope, ['kind', 'schemaVersion', 'exportedAt', 'state'], '导入文件');
  if (envelope.kind !== DEMO_EXPORT_KIND || envelope.schemaVersion !== 1)
    throw new Error('仅支持本应用导出的第 1 版演示记录，不支持数据库或其他文件。');
  return {
    state: parseDemoState(envelope.state),
    exportedAt: timestamp(envelope.exportedAt, '导出时间'),
  };
}
