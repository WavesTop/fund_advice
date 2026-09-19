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
  Database,
  Monitor,
  ChevronRight,
  CircleHelp,
} from 'lucide-react';
import { EmptyState } from '../shared/ui';
import './styles.css';

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
  import('../features/portfolio/RealPortfolioPage').then((module) => ({
    default: module.RealPortfolioPage,
  })),
);
const AdvicePage = lazy(() =>
  import('../features/advice/StrategyAdvicePage').then((module) => ({ default: module.StrategyAdvicePage })),
);
const ReviewPage = lazy(() =>
  import('../features/review/RealReviewPage').then((module) => ({ default: module.RealReviewPage })),
);
const SettingsPage = lazy(() =>
  import('../features/settings/RealSettingsPage').then((module) => ({ default: module.RealSettingsPage })),
);

const navigation = [
  { to: '/funds', label: '基金与板块', caption: '发现投资机会', icon: Layers3 },
  { to: '/portfolio', label: '我的持仓', caption: '看清资产与记录', icon: Wallet },
  { to: '/advice', label: '建议与决策', caption: '让每次选择有据可循', icon: Compass },
  { to: '/review', label: '收益与复盘', caption: '比较、验证与改良', icon: ChartNoAxesCombined },
];
function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [query, setQuery] = useState('');
  const isMarket = /^\/(funds|sectors)/.test(location.pathname);
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
            to="/settings"
            aria-label="查看本地服务与数据状态"
          >
            <Database size={18} />
            <span>数据状态</span>
          </Link>
        </header>
        <div className="data-banner">
          <span className="data-tag">本机真实数据</span>
          <span>基金、板块、持仓、策略建议和历史验证均读取本地正式接口；资料缺失会明确保留为空。</span>
          <Link to="/settings">核对数据状态 <ArrowUpRight size={13} /></Link>
        </div>
        <main id="main-content" className="main-content" tabIndex={-1}>
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
                    description="可以返回基金库继续浏览真实目录和已采集的历史资料。"
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
            <CircleHelp size={13} /> 本地正式账本 · 可追溯建议 · 真实历史验证
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
          <p>页面未能继续运行。正式数据仍保存在本地数据库中，请刷新重试。</p>
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
    element: <Shell />,
    errorElement: (
      <div className="fatal-error">
        <h1>页面暂时无法加载</h1>
        <p>请重新加载页面。本地数据库中的正式记录不会受到影响。</p>
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
