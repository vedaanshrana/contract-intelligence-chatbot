import { BotMessageSquare, BrainCircuit, LayoutDashboard, Moon, Sun } from 'lucide-react'
import { AppProvider, useApp } from './store'
import LoginGateway from './components/auth/LoginGateway'
import Sidebar from './components/layout/Sidebar'
import SettingsModal from './components/layout/SettingsModal'
import DashboardView from './components/dashboard/DashboardView'
import AgentsView from './components/agents/AgentsView'
import ChatbotView from './components/chatbot/ChatbotView'
import type { TabId } from './types'

const TABS: { id: TabId; label: string; icon: React.ElementType }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { id: 'agents', label: 'Agents Intelligence', icon: BrainCircuit },
  { id: 'chat', label: 'Cognitive Chat', icon: BotMessageSquare },
]

function TopBar() {
  const { activeTab, setActiveTab, theme, toggleTheme, core, scopeLabel } = useApp()
  return (
    <header className="flex shrink-0 items-center justify-between border-b border-line bg-surface px-4 py-2">
      {/* context breadcrumb */}
      <div className="hidden min-w-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-ink-3 lg:flex">
        <span className="text-primary">●</span>
        <span className="truncate">{core}</span>
        <span className="text-line-strong">/</span>
        <span className="truncate text-ink-2">{scopeLabel}</span>
      </div>

      {/* segmentation tab bar */}
      <div className="flex border border-line bg-surface-2/60 p-[3px]">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={`flex items-center gap-2 px-4 py-1.5 text-[12px] font-medium transition-colors duration-150 ${
              activeTab === id
                ? 'bg-surface text-ink shadow-sm'
                : 'text-ink-3 hover:text-ink-2'
            }`}
          >
            <Icon size={13} className={activeTab === id ? 'text-primary' : ''} />
            {label}
          </button>
        ))}
      </div>

      {/* right controls */}
      <div className="flex items-center gap-2">
        <span className="hidden font-mono text-[9px] uppercase tracking-wider text-ink-3 md:block">
          ENV · PROD
        </span>
        <button
          onClick={toggleTheme}
          className="flex h-7 w-7 items-center justify-center border border-line text-ink-3 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
          aria-label="Toggle theme"
        >
          {theme === 'dark' ? <Sun size={13} /> : <Moon size={13} />}
        </button>
      </div>
    </header>
  )
}

function Workspace() {
  const { activeTab } = useApp()
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <TopBar />
      <main className="min-h-0 min-w-0 flex-1">
        {activeTab === 'dashboard' && <DashboardView />}
        {activeTab === 'agents' && <AgentsView />}
        {activeTab === 'chat' && <ChatbotView />}
      </main>
    </div>
  )
}

function Shell() {
  const { user } = useApp()
  if (!user) return <LoginGateway />
  return (
    <div className="flex h-full overflow-hidden bg-bg text-ink">
      <Sidebar />
      <Workspace />
      <SettingsModal />
    </div>
  )
}

export default function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  )
}
