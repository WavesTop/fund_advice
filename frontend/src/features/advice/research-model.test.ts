import { describe, expect, it } from 'vitest';
import { linkedFixture, researchFixture } from './research-fixtures';
import {
  isDemoAdvice, parseFundIdentity, parseResearchResponse, periodObservations, percent, researchHref,
  safeReturnPath, selectResearchItems, selectionFrom, sourceHref, subjectKey,
} from './research-model';

const none = selectionFrom(new URLSearchParams());
describe('real research identity and display contracts', () => {
  it('accepts the current contract without mutating the source', () => {
    const data = linkedFixture();
    const previous = structuredClone(data);
    expect(parseResearchResponse(data)).toEqual(previous);
    expect(data).toEqual(previous);
  });
  it('does not require the new optional association fields from an older backend', () => {
    expect(parseResearchResponse(researchFixture()).items).toHaveLength(1);
  });
  it('rejects malformed or duplicated observations', () => {
    expect(() => parseResearchResponse({})).toThrow();
    const data = researchFixture();
    data.items.push(structuredClone(data.items[0]));
    expect(() => parseResearchResponse(data)).toThrow(/身份重复/);
  });
  it('never substitutes another fund or infers a relationship by name', () => {
    const data = researchFixture();
    data.items[0].funds = [{ code: '000001', name: '测试基金' }];
    expect(selectResearchItems(data.items, { ...none, fund: '000001' }).items).toEqual([]);
    expect(selectResearchItems(linkedFixture().items, { ...none, fund: '999999' }).problem).toContain('999999');
  });
  it('allows only the exact verified relationship', () => {
    const data = linkedFixture();
    expect(selectResearchItems(data.items, { ...none, fund: '000001' }).items).toHaveLength(1);
    data.items[0].research!.fund_associations[0].status = 'withheld';
    expect(selectResearchItems(data.items, { ...none, fund: '000001' }).items).toHaveLength(0);
  });
  it('separates equal codes in different sources and rejects ambiguous old links', () => {
    const data = researchFixture();
    data.items.push({ ...structuredClone(data.items[0]), source_id: 'other.source' });
    expect(subjectKey(data.items[0])).not.toBe(subjectKey(data.items[1]));
    expect(selectResearchItems(data.items, { ...none, sector: 'BK_TEST' }).problem).toContain('多个来源');
    expect(selectResearchItems(data.items, { ...none, sector: 'BK_TEST', source: 'fixture.board' }).items).toHaveLength(1);
  });
  it('keeps missing and stale objects out of the default observation list', () => {
    const data = researchFixture();
    expect(periodObservations(data, 'short', none)).toHaveLength(1);
    data.items[0].periods[0].status = 'stale';
    expect(periodObservations(data, 'short', none)).toHaveLength(0);
  });
  it('keeps stale evidence visible when explicitly inspecting its object', () => {
    const data = researchFixture();
    data.items[0].collection_error = 'test outage';
    expect(periodObservations(data, 'short', none)).toHaveLength(0);
    expect(periodObservations(data, 'short', { ...none, sector: 'BK_TEST' })).toHaveLength(1);
  });
  it('does not blend tracked indexes into an industry observation rank', () => {
    expect(periodObservations(linkedFixture(), 'short', none)).toHaveLength(0);
  });
  it('preserves identity across deep links', () => {
    const item = researchFixture().items[0];
    const params = new URL(researchHref(item), 'https://example.org').searchParams;
    expect(selectResearchItems([item], selectionFrom(params)).items).toEqual([item]);
  });
  it('rejects a mismatched fund response', () => {
    expect(() => parseFundIdentity({ fund: { code: '999999' } }, '000001')).toThrow(/身份/);
  });
  it('restricts returns to internal known pages and source links to http(s)', () => {
    expect(safeReturnPath('//example.org', '/funds')).toBe('/funds');
    expect(safeReturnPath('/funds?q=test&page=2', '/funds')).toBe('/funds?q=test&page=2');
    expect(sourceHref('javascript:alert(1)')).toBeUndefined();
    expect(sourceHref('https://name:password@example.org')).toBeUndefined();
    expect(sourceHref('https://example.org/source')).toBe('https://example.org/source');
  });
  it('uses real research by default and preserves explicitly selected legacy demonstrations', () => {
    expect(isDemoAdvice(new URLSearchParams())).toBe(false);
    expect(isDemoAdvice(new URLSearchParams('fund=000001'))).toBe(false);
    expect(isDemoAdvice(new URLSearchParams('mode=demo'))).toBe(true);
    expect(isDemoAdvice(new URLSearchParams('tab=decisions'))).toBe(true);
    expect(isDemoAdvice(new URLSearchParams('tab=history'))).toBe(true);
  });
  it('does not turn unknown metrics into zero', () => {
    expect(percent(null)).toBe('—');
    expect(percent(Number.NaN)).toBe('—');
    expect(percent(0)).toBe('0.00%');
    expect(percent(3)).toBe('+3.00%');
  });
});
