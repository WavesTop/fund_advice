import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useState,
  type ErrorInfo,
  type ReactNode,
  type FormEvent,
} from 'react';
import {
  createBrowserRouter,
  RouterProvider,
  NavLink,
  Navigate,
  Route,
  Routes,
  Link,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import {
  Layers3,
  Wallet,
  Compass,
  ChartNoAxesCombined,
  Settings2,
  Search,
  ArrowUpRight,
  Menu,
  X,
  ListTodo,
  Monitor,
  ChevronRight,
  CircleHelp,
} from 'lucide-react';
import { DemoProvider, useDemo } from '../shared/store';
import { EmptyState, Notice } from '../shared/ui';
import './styles.css';
import { isDemoAdvice } from '../features/advice/research-model';

const FundsPage = lazy(() =>
  import('../features/market/MarketPages').then((module) => ({ default: module.FundsPage })),
);
const FundDetailPage = lazy(() =>
  import('../features/market/MarketPages').then((module) => ({ default: module.FundDetailPage })),
);
const SectorsPage = lazy(() =>
  import('../features/market/MarketPages').then((module) => ({ default: module.SectorsPage })),
);
const SectorDetailPage = lazy(() =>
  import('../features/market/MarketPages').then((module) => ({ default: module.SectorDetailPage })),
);
const PortfolioPage = lazy(() =>
  import('../features/portfolio/PortfolioPage').then((module) => ({
    default: module.PortfolioPage,
  })),
);
const AdvicePage = lazy(() =>
  import('../features/advice/AdvicePage').then((module) => ({ default: module.AdvicePage })),
);
const ReviewPage = lazy(() =>
  import('../features/review/ReviewPage').then((module) => ({ default: module.ReviewPage })),
);
const SettingsPage = lazy(() =>
  import('../features/settings/SettingsPage').then((module) => ({ default: module.SettingsPage })),
);

const navigation = [
  { to: '/funds', label: '基金与板块', caption: '发现投资机会', icon: Layers3 },
  { to: '/portfolio', label: '我的持仓', caption: '看清资产与记录', icon: Wallet },
  { to: '/advice', label: '建议与决策', caption: '让每次选择有据可循', icon: Compass },
  { to: '/review', label: '收益与复盘', caption: '比较、验证与改良', icon: ChartNoAxesCombined },
];
const scenarioNotices = {
  ready: '',
  empty: '空数据演示：展示首次使用、资料缺失与补全入口。',
  loading: '加载状态演示：不会自动伪装数据已下载。可在设置中切回正常场景。',
  offline: '断网状态演示：仍可查看已有示例资料，外部更新不可执行。',
  error: '失败状态演示：保存会失败并保留输入。请到设置切换场景后重试。',
  stale: '资料过期演示：结果仅供界面审查，示例日期为 2026-09-11。',
  'ai-conflict': '判断冲突演示：本地与 AI 意见分别显示，尚未形成一致结论。',
};
function Shell() {
  const { state } = useDemo();
  const location = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [query, setQuery] = useState('');
  const isMarket = /^\/(funds|sectors)/.test(location.pathname);
  const isRealFundArea = location.pathname === '/funds' || location.pathname.startsWith('/funds/');
  const isSectorOpportunityArea = location.pathname === '/sectors';
  const isResearchArea = location.pathname === '/advice' && !isDemoAdvice(new URLSearchParams(location.search));
  const active = navigation.find((item) =>
    item.to === '/funds' ? isMarket : location.pathname.startsWith(item.to),
  );
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname, location.search]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);
  const search = (event: FormEvent) => {
    event.preventDefault();
    navigate(`/funds?q=${encodeURIComponent(query.trim())}`);
  };
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        跳至主要内容
      </a>
      {menuOpen && (
        <button
          className="nav-scrim"
          type="button"
          aria-label="关闭导航菜单"
          onClick={() => setMenuOpen(false)}
        />
      )}
      <aside className={`sidebar ${menuOpen ? 'sidebar-open' : ''}`} id="main-navigation">
        <Link to="/funds" className="brand" aria-label="知衡首页">
          <span className="brand-mark">
            <ChartNoAxesCombined size={24} />
          </span>
          <span className="brand-text">
            知衡<small>FUND INSIGHT</small>
          </span>
        </Link>
        <div className="nav-section-label">投资工作台</div>
        <nav aria-label="主导航">
          {navigation.map(({ to, label, caption, icon: Icon }) => (
            <NavLink
              to={to}
              key={to}
              className={({ isActive }) =>
                `nav-item ${(to === '/funds' ? isMarket : isActive) ? 'nav-item-active' : ''}`
              }
              aria-label={label}
            >
              <Icon size={19} />
              <span>
                <strong>{label}</strong>
                <small>{caption}</small>
              </span>
              <ChevronRight size={13} className="nav-chevron" />
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <span className="mini-line" />
            观察市场，
            <br />
            也观察自己的判断。<p>从发现机会到持续复盘</p>
          </div>
          <NavLink to="/settings" aria-label="设置" className="nav-item settings-nav">
            <Settings2 size={19} />
            <span>设置</span>
          </NavLink>
          <div className="local-label">
            <Monitor size={14} />
            <span>
              本机工作台 <i />
            </span>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="topbar-start">
            <button
              className="icon-button menu-toggle"
              type="button"
              aria-label={menuOpen ? '收起导航' : '展开导航'}
              aria-expanded={menuOpen}
              aria-controls="main-navigation"
              onClick={() => setMenuOpen(!menuOpen)}
            >
              {menuOpen ? <X size={20} /> : <Menu size={20} />}
            </button>
            <span className="breadcrumb">
              工作台 <ChevronRight size={13} /> <strong>{active?.label ?? '设置'}</strong>
            </span>
          </div>
          <form className="global-search" role="search" onSubmit={search}>
            <Search size={16} />
            <input
              aria-label="全局基金搜索"
              placeholder="搜索基金名称或代码"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="submit" aria-label="搜索基金">
              <ArrowUpRight size={16} />
            </button>
          </form>
          <Link
            className="task-link"
            to="/settings?tab=tasks"
            aria-label={`任务记录，${state.tasks.filter((task) => task.status === '排队中').length} 项排队`}
          >
            <ListTodo size={18} />
            <span>任务记录</span>
            {state.tasks.some((task) => task.status === '排队中') && <i />}
          </Link>
        </header>
        <div className="demo-banner">
          <span className="demo-tag">{isRealFundArea ? '真实基金数据' : isSectorOpportunityArea ? '板块证据观察' : isResearchArea ? '真实投资研究' : '界面演示'}</span>
          <span>
            {isRealFundArea
              ? '基金目录及已采集序列来自本地数据库；未采集内容会明确留空。'
              : isSectorOpportunityArea
                ? '真实行情与已核验行业快照；请结合资料日期、适用范围和反证阅读。'
                : isResearchArea
                  ? '读取真实本地资料；历史走势、研究证据、基金关联与正式推荐分别展示。'
                  : '虚构示例资料 · 输入仅保存在本浏览器的演示草稿中，请勿录入真实隐私或密钥。'}
          </span>
          {!isRealFundArea && !isSectorOpportunityArea && !isResearchArea && <Link to="/settings?tab=demo">切换演示场景 <ArrowUpRight size={13} /></Link>}
        </div>
        <main id="main-content" className="main-content" tabIndex={-1}>
          {state.scenario !== 'ready' && !isSectorOpportunityArea && !isRealFundArea && !isResearchArea && (
            <Notice tone={state.scenario === 'error' ? 'error' : 'warning'}>
              {scenarioNotices[state.scenario]} <Link to="/settings?tab=demo">切换场景</Link>
            </Notice>
          )}
          <Suspense
            fallback={
              <div className="page-loading" role="status">
                <span className="loading-ring" />
                正在载入界面…
              </div>
            }
          >
            <Routes>
              <Route path="/" element={<Navigate to="/funds" replace />} />
              <Route path="/funds" element={<FundsPage />} />
              <Route path="/funds/:code" element={<FundDetailPage />} />
              <Route path="/sectors" element={<SectorsPage />} />
              <Route path="/sectors/:sector" element={<SectorDetailPage />} />
              <Route path="/portfolio/*" element={<PortfolioPage />} />
              <Route path="/advice/*" element={<AdvicePage />} />
              <Route path="/review/*" element={<ReviewPage />} />
              <Route path="/settings/*" element={<SettingsPage />} />
              <Route
                path="*"
                element={
                  <EmptyState
                    title="这个页面暂不存在"
                    description="可以返回基金库继续浏览，已保存的演示记录仍然保留。"
                    action={
                      <Link className="button primary" to="/funds">
                        返回基金库
                      </Link>
                    }
                  />
                }
              />
            </Routes>
          </Suspense>
        </main>
        <footer className="app-footer">
          <span>知衡 · 让投资判断留下依据</span>
          <span>
            <CircleHelp size={13} /> {isRealFundArea ? '真实基金目录与本地时序数据' : '其他功能仍处于界面演示阶段'}
          </span>
        </footer>
      </div>
    </div>
  );
}
class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Interface error', error, info.componentStack);
  }
  render() {
    if (this.state.failed)
      return (
        <div className="fatal-error">
          <h1>界面遇到了问题</h1>
          <p>已保存的演示草稿仍保留在本浏览器中。请刷新重试。</p>
          <button className="button primary" onClick={() => window.location.reload()}>
            重新加载
          </button>
        </div>
      );
    return this.props.children;
  }
}
const router = createBrowserRouter([
  {
    path: '*',
    element: (
      <DemoProvider>
        <Shell />
      </DemoProvider>
    ),
    errorElement: (
      <div className="fatal-error">
        <h1>页面暂时无法加载</h1>
        <p>请重新加载页面。已保存的演示记录仍保留在浏览器中。</p>
        <a className="button primary" href="/funds">
          重新打开基金库
        </a>
      </div>
    ),
  },
]);
export default function App() {
  return (
    <ErrorBoundary>
      <RouterProvider router={router} />
    </ErrorBoundary>
  );
}
