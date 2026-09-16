/** Visits are recorded only after a real detail request succeeds. Not search history. */
export const fundVisitKey = 'fundAdvicer.realFundVisits.v1';
export const fundVisitEvent = 'fund-advice:fund-visit';
const limit = 60;

export function readFundVisits(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(fundVisitKey) ?? '[]');
    if (!Array.isArray(value)) return [];
    return [...new Set(value.filter((code): code is string =>
      typeof code === 'string' && /^[0-9]{6}$/.test(code)))].slice(0, limit);
  } catch { return []; }
}

export function recordFundVisit(code: string): void {
  if (!/^[0-9]{6}$/.test(code)) return;
  const codes = [code, ...readFundVisits().filter((previous) => previous !== code)].slice(0, limit);
  try {
    localStorage.setItem(fundVisitKey, JSON.stringify(codes));
    window.dispatchEvent(new Event(fundVisitEvent));
  } catch { /* Optional local history must not prevent viewing a real fund. */ }
}
