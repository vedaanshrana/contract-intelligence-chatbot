import { useMemo, useState } from 'react'
import {
  Boxes,
  Check,
  CheckSquare,
  FolderClosed,
  LayoutGrid,
  ListTodo,
  LogOut,
  MessageSquareText,
  MousePointerClick,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search,
  Settings,
  Trash2,
  X,
} from 'lucide-react'
import { useApp } from '../../store'
import type { ChatSession, ClientStatus } from '../../types'
import Dropdown from '../ui/Dropdown'
import Tooltip from '../ui/Tooltip'

/* ── Collapsed icon rail ────────────────────────────────────────── */
function CollapsedRail() {
  const { setSidebarCollapsed, setSettingsOpen, setActiveTab, user } = useApp()
  const items = [
    { icon: Boxes, label: 'Core Context', act: () => setSidebarCollapsed(false) },
    { icon: FolderClosed, label: 'Client Navigator', act: () => setSidebarCollapsed(false) },
    { icon: MessageSquareText, label: 'Recent Chats', act: () => { setSidebarCollapsed(false); setActiveTab('chat') } },
    { icon: Settings, label: 'Settings', act: () => setSettingsOpen(true) },
  ]
  return (
    <div className="flex h-full flex-col items-center gap-1 py-3">
      <Tooltip label="Expand Sidebar">
        <button
          onClick={() => setSidebarCollapsed(false)}
          className="mb-2 flex h-8 w-8 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
          aria-label="Expand sidebar"
        >
          <PanelLeftOpen size={15} />
        </button>
      </Tooltip>
      {items.map(({ icon: Icon, label, act }) => (
        <Tooltip key={label} label={label}>
          <button
            onClick={act}
            className="flex h-8 w-8 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
            aria-label={label}
          >
            <Icon size={15} />
          </button>
        </Tooltip>
      ))}
      <div className="flex-1" />
      <Tooltip label={user?.name ?? ''}>
        <button
          onClick={() => setSidebarCollapsed(false)}
          className="flex h-8 w-8 items-center justify-center bg-primary-dim font-mono text-[10px] font-semibold text-primary"
        >
          {user?.name.split(' ').map((p) => p[0]).join('')}
        </button>
      </Tooltip>
    </div>
  )
}

/* ── Status pip ─────────────────────────────────────────────────── */
function StatePip({ state }: { state: ClientStatus['state'] }) {
  const cls =
    state === 'done' ? 'bg-ok' : state === 'partial' ? 'bg-warn' : 'bg-line-strong'
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${cls}`} />
}

/* ── Client navigator (real clients for the selected core) ──────── */
function ClientNavigator() {
  const {
    clients,
    clientsLoading,
    refreshClients,
    selectMode,
    setSelectMode,
    selectedIds,
    toggleClient,
    clearSelection,
    selectAll,
    core,
  } = useApp()
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? clients.filter((c) => c.client.toLowerCase().includes(q)) : clients
  }, [clients, query])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* search + controls */}
      <div className="shrink-0 px-3 pb-2">
        <div className="flex items-center gap-1.5">
          <div className="flex flex-1 items-center gap-1.5 border border-line bg-surface-2 px-2 py-1.5 transition-colors duration-150 focus-within:border-primary">
            <Search size={12} className="shrink-0 text-ink-3" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`Search ${clients.length} clients…`}
              className="w-full bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-3"
            />
            {query && (
              <button onClick={() => setQuery('')} aria-label="Clear search">
                <X size={12} className="text-ink-3 transition-colors duration-150 hover:text-ink" />
              </button>
            )}
          </div>
          <button
            onClick={refreshClients}
            title="Refresh client status"
            className="flex h-[30px] w-[30px] shrink-0 items-center justify-center border border-line text-ink-3 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
            aria-label="Refresh clients"
          >
            <RefreshCw size={12} className={clientsLoading ? 'animate-spin' : ''} />
          </button>
        </div>

        {/* selection mode + count */}
        <div className="mt-1.5 flex items-center justify-between">
          <div className="flex border border-line p-[2px]">
            {(
              [
                ['single', MousePointerClick, 'Single select'],
                ['multi', CheckSquare, 'Multi select'],
              ] as const
            ).map(([m, Icon, label]) => (
              <button
                key={m}
                title={label}
                onClick={() => setSelectMode(m)}
                className={`flex items-center gap-1 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider transition-colors duration-150 ${
                  selectMode === m
                    ? 'bg-primary-dim text-primary'
                    : 'text-ink-3 hover:text-ink-2'
                }`}
              >
                <Icon size={10} />
                {m}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2.5 font-mono text-[10px]">
            {selectMode === 'multi' && filtered.length > 0 && (
              <button
                onClick={() => selectAll(filtered.map((c) => c.client))}
                className="text-ink-3 transition-colors duration-150 hover:text-primary"
                title={query ? `Select the ${filtered.length} matching clients` : 'Select all clients'}
              >
                select all
              </button>
            )}
            {selectedIds.size > 0 && (
              <button
                onClick={clearSelection}
                className="text-ink-3 transition-colors duration-150 hover:text-bad"
              >
                {selectedIds.size} selected · clear
              </button>
            )}
          </div>
        </div>
      </div>

      {/* scrollable client list */}
      <div className="min-h-0 flex-1 overflow-y-auto border-y border-line bg-bg/40 px-1.5 py-1">
        {clientsLoading && clients.length === 0 && (
          <div className="px-3 py-6 text-center font-mono text-[10px] uppercase tracking-wider text-ink-3">
            Loading {core}…
          </div>
        )}
        {!clientsLoading && filtered.length === 0 && (
          <div className="px-3 py-6 text-center text-[11px] text-ink-3">
            {query ? `No clients match “${query}”` : 'No clients in this core'}
          </div>
        )}
        {filtered.map((c) => {
          const sel = selectedIds.has(c.client)
          return (
            <button
              key={c.client}
              onClick={() => toggleClient(c.client)}
              className={`flex w-full items-center gap-2 py-[5px] pl-2.5 pr-2 text-left transition-colors duration-150 ${
                sel && selectMode === 'single'
                  ? 'border-l-2 border-primary bg-primary-dim'
                  : 'border-l-2 border-transparent hover:bg-surface-2'
              }`}
            >
              {selectMode === 'multi' ? (
                <span
                  className={`flex h-[13px] w-[13px] shrink-0 items-center justify-center border transition-colors duration-150 ${
                    sel ? 'border-primary bg-primary text-white' : 'border-line-strong bg-surface'
                  }`}
                >
                  {sel && <Check size={9} strokeWidth={3.5} />}
                </span>
              ) : (
                <StatePip state={c.state} />
              )}
              <span className={`flex-1 truncate text-[11.5px] ${sel ? 'font-medium text-ink' : 'text-ink-2'}`}>
                {c.client}
              </span>
              <span
                className={`font-mono text-[9px] ${
                  c.state === 'done' ? 'text-ok' : c.state === 'partial' ? 'text-warn' : 'text-ink-3'
                }`}
              >
                {c.agentsDone}/{c.agentsTotal}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/* ── Chat session recents ───────────────────────────────────────── */
function groupOf(updatedAt: number): 'Today' | 'Yesterday' | 'Previous 7 Days' | 'Older' {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayMs = 86_400_000
  if (updatedAt >= startOfToday) return 'Today'
  if (updatedAt >= startOfToday - dayMs) return 'Yesterday'
  if (updatedAt >= startOfToday - 7 * dayMs) return 'Previous 7 Days'
  return 'Older'
}

function ChatHistory() {
  const { sessions, activeSessionId, setActiveSessionId, setActiveTab, deleteSession } = useApp()
  const groups = ['Today', 'Yesterday', 'Previous 7 Days', 'Older'] as const
  const byGroup = useMemo(() => {
    const m = new Map<string, ChatSession[]>()
    for (const g of groups) m.set(g, [])
    for (const s of sessions) m.get(groupOf(s.updatedAt))?.push(s)
    return m
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions])

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 px-3 pb-1 pt-2.5">
        <ListTodo size={11} className="text-ink-3" />
        <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-ink-3">Recents</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-1">
        {sessions.length === 0 && (
          <div className="px-2.5 py-3 text-[11px] text-ink-3">
            No conversations yet. Start one in Cognitive Chat.
          </div>
        )}
        {groups.map((g) => {
          const items = byGroup.get(g) ?? []
          if (items.length === 0) return null
          return (
            <div key={g}>
              <div className="px-1.5 pb-0.5 pt-2 font-mono text-[9px] uppercase tracking-wider text-ink-3/80">
                {g}
              </div>
              {items.map((c) => (
                <div
                  key={c.id}
                  className={`group flex items-center gap-1 ${
                    activeSessionId === c.id ? 'bg-primary-dim' : 'hover:bg-surface-2'
                  }`}
                >
                  <button
                    onClick={() => {
                      setActiveSessionId(c.id)
                      setActiveTab('chat')
                    }}
                    className="block min-w-0 flex-1 px-1.5 py-[5px] text-left"
                  >
                    <span
                      className={`fade-truncate block text-[11.5px] ${
                        activeSessionId === c.id ? 'text-primary' : 'text-ink-2'
                      }`}
                    >
                      {c.title}
                    </span>
                  </button>
                  <button
                    onClick={() => deleteSession(c.id)}
                    className="shrink-0 pr-1.5 text-ink-3 opacity-0 transition-opacity duration-150 hover:text-bad group-hover:opacity-100"
                    aria-label="Delete conversation"
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ── Sidebar shell ──────────────────────────────────────────────── */
export default function Sidebar() {
  const {
    sidebarCollapsed,
    setSidebarCollapsed,
    cores,
    core,
    setCore,
    setSettingsOpen,
    user,
    logout,
  } = useApp()

  return (
    <aside
      className={`flex h-full shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-150 ${
        sidebarCollapsed ? 'w-[52px]' : 'w-[284px]'
      }`}
    >
      {sidebarCollapsed ? (
        <CollapsedRail />
      ) : (
        <>
          <div className="flex shrink-0 items-center justify-between px-3 py-3">
            <span className="text-[13px] font-semibold tracking-tight text-ink">CONTRACT INTELLIGENCE</span>
            <button
              onClick={() => setSidebarCollapsed(true)}
              className="flex h-7 w-7 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
              aria-label="Collapse sidebar"
            >
              <PanelLeftClose size={14} />
            </button>
          </div>

          {/* core selector */}
          <div className="shrink-0 px-3 pb-3">
            <div className="pb-1 font-mono text-[9px] uppercase tracking-[0.16em] text-ink-3">Core</div>
            <Dropdown
              value={core}
              onChange={setCore}
              options={cores.map((c) => ({ value: c.name, label: c.name, hint: `${c.clients} clients` }))}
              renderButton={(sel) => (
                <span className="flex min-w-0 items-center gap-2">
                  <LayoutGrid size={13} className="shrink-0 text-primary" />
                  <span className="truncate text-[12px] font-medium">{sel?.label ?? 'Select core'}</span>
                </span>
              )}
            />
          </div>

          <ClientNavigator />

          <div className="flex min-h-0 basis-[30%] flex-col">
            <ChatHistory />
          </div>

          {/* profile footer */}
          <div className="shrink-0 border-t border-line px-3 py-2.5">
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center bg-primary-dim font-mono text-[10px] font-semibold text-primary">
                {user?.name.split(' ').map((p) => p[0]).join('')}
              </div>
              <div className="min-w-0 flex-1 leading-tight">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-[11.5px] font-semibold text-ink">{user?.name}</span>
                  {user?.role === 'admin' && (
                    <span className="shrink-0 bg-primary-dim px-1 font-mono text-[8px] uppercase tracking-wider text-primary">
                      Admin
                    </span>
                  )}
                </div>
                <div className="truncate font-mono text-[9.5px] text-ink-3">{user?.email}</div>
                <div className="truncate font-mono text-[9.5px] text-ink-3">{user?.department}</div>
              </div>
              <div className="flex shrink-0 flex-col gap-0.5">
                <button
                  onClick={() => setSettingsOpen(true)}
                  className="flex h-6 w-6 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-primary"
                  aria-label="Open settings"
                >
                  <Settings size={13} />
                </button>
                <button
                  onClick={logout}
                  className="flex h-6 w-6 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-bad"
                  aria-label="Log out"
                >
                  <LogOut size={13} />
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </aside>
  )
}
