export type ApiFailure = {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
  detail?: string | Array<{ msg?: string }>;
};

export class BackendError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly details?: unknown;

  constructor(message: string, status: number, code?: string, details?: unknown) {
    super(message);
    this.name = 'BackendError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function errorFrom(response: Response): Promise<BackendError> {
  const body = (await response.json().catch(() => null)) as ApiFailure | null;
  const validation = Array.isArray(body?.detail)
    ? body.detail.map((item) => item.msg).filter(Boolean).join('；')
    : body?.detail;
  return new BackendError(
    body?.error?.message || validation || `本地数据服务返回 HTTP ${response.status}`,
    response.status,
    body?.error?.code,
    body?.error?.details,
  );
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, signal: signal ?? init.signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new BackendError('无法连接本地数据服务，请确认应用服务已启动。', 0);
  }
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as T;
}

export function jsonRequest(body: unknown, idempotencyKey?: string): RequestInit {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  return { method: 'POST', headers, body: JSON.stringify(body) };
}

export function requestKey(prefix: string): string {
  const suffix = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

export function localDate(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function formatMoney(value: string | number | null | undefined): string {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

export function formatPercent(value: string | number | null | undefined): string {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return `${number >= 0 ? '+' : ''}${number.toFixed(2)}%`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(parsed);
}

export function formatPlainPercent(value: string | number | null | undefined): string {
  if (value == null || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2)}%` : '—';
}

export function formatCount(value: string | number | null | undefined): string {
  if (value == null || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : '—';
}
