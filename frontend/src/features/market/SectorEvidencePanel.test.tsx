import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { SectorEvidencePanel } from './SectorEvidencePanel';
import { evidenceFixture } from './sector-evidence-fixtures';
afterEach(cleanup);
describe('sector collection diagnostics', () => {
  it('renders known sample values, source links and honest history gaps', () => {
    render(<SectorEvidencePanel name="测试行业" evidence={evidenceFixture()} />);
    expect(screen.getByRole('region', { name: '测试行业资料采集与覆盖' })).toBeInTheDocument();
    expect(screen.getByText('50.00%')).toBeInTheDocument();
    expect(screen.getByText('历史不足，暂不判断便宜/昂贵')).toBeInTheDocument();
    fireEvent.click(screen.getByText('来源、行业专属待采集项与计算边界'));
    expect(screen.getByRole('link', { name: '来源响应 1' })).toHaveAttribute('href', 'https://example.test/data');
  });
  it('does not supply sample values for an older backend without evidence', () => {
    render(<SectorEvidencePanel name="测试行业" />);
    expect(screen.getByText(/当前后端尚未返回新版成分证据/)).toBeInTheDocument();
    expect(screen.queryByText('25.00 倍')).not.toBeInTheDocument();
  });
  it.each(['not_collected', 'membership_mismatch'])('explains collection limits when evidence is %s', (status) => {
    const evidence = evidenceFixture();
    evidence.scope = '当前同花顺行业成分样本（非官方指数加权）';
    evidence.status = status;
    evidence.operating = { status, periods: [] };
    evidence.valuation = { status, percentile: null, history_count: 0, history_span_days: 0 };
    render(<SectorEvidencePanel name="测试行业" evidence={evidence} />);
    expect(screen.getByText(/尚未接入成分股财报/)).toBeInTheDocument();
    expect(screen.getByText(/尚未接入带业务日期和明确口径的估值/)).toBeInTheDocument();
    expect(screen.queryByText(/重新评估会尝试获取/)).not.toBeInTheDocument();
  });
  it('keeps index-fact empty states neutral about provider collection capabilities', () => {
    const evidence = evidenceFixture();
    evidence.scope = '已入库且通过时点核验的指数范围事实';
    evidence.operating = { status: 'not_collected', periods: [] };
    evidence.valuation = { status: 'not_collected', percentile: null, history_count: 0, history_span_days: 0 };
    render(<SectorEvidencePanel name="测试指数" evidence={evidence} />);
    expect(screen.getByText('未取得可比经营样本。')).toBeInTheDocument();
    expect(screen.getByText('未取得带业务日期和明确口径的估值资料。')).toBeInTheDocument();
    expect(screen.queryByText(/当前同花顺采集链路/)).not.toBeInTheDocument();
    expect(screen.queryByText(/重新评估会尝试获取/)).not.toBeInTheDocument();
  });
  it.each(['scope', 'source'])('does not present retired evidence identified by %s as current data', (field) => {
    const evidence = evidenceFixture();
    if (field === 'scope') evidence.scope = '当前东方财富成分样本（非官方指数加权）';
    else evidence.sources = [{ url: 'https://datacenter-web.eastmoney.com/api/data', asset_id: 'retired' }];
    render(<SectorEvidencePanel name="测试行业" evidence={evidence} />);
    expect(screen.getByText(/来源声明与同花顺配置不一致，已暂停展示这份资料/)).toBeInTheDocument();
    expect(screen.queryByText(/当前东方财富成分样本/)).not.toBeInTheDocument();
    expect(screen.queryByText('50.00%')).not.toBeInTheDocument();
    expect(screen.queryByText('25.00 倍')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
