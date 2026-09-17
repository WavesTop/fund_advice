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
});
