import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type {
  BackendConfig,
  ChatSession,
  ClientStatus,
  CoreInfo,
  Settings,
  TabId,
  User,
} from './types'
import { api } from './api'

export type SelectMode = 'single' | 'multi'

const SESS_KEY = 'contraxis.sessions.v1'

function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(SESS_KEY)
    return raw ? (JSON.parse(raw) as ChatSession[]) : []
  } catch {
    return []
  }
}
function persistSessions(s: ChatSession[]) {
  try {
    localStorage.setItem(SESS_KEY, JSON.stringify(s.slice(0, 50)))
  } catch {
    /* quota — ignore */
  }
}

interface Store {
  user: User | null
  login: (u: User) => void
  logout: () => void

  theme: 'dark' | 'light'
  toggleTheme: () => void

  sidebarCollapsed: boolean
  setSidebarCollapsed: (v: boolean) => void

  /* backend config + settings */
  config: BackendConfig | null
  settings: Settings | null
  saveSettings: (patch: Partial<Settings>) => Promise<void>

  /* cores + clients (real data) */
  cores: CoreInfo[]
  core: string
  setCore: (c: string) => void
  clients: ClientStatus[]
  clientsLoading: boolean
  refreshClients: () => void

  /* selection — keyed by client name */
  selectMode: SelectMode
  setSelectMode: (m: SelectMode) => void
  selectedIds: Set<string>
  toggleClient: (name: string) => void
  clearSelection: () => void
  /** Select every client (or a given subset, e.g. the current search results). */
  selectAll: (names?: string[]) => void

  /** Selected client statuses, or all clients in the core when none selected */
  scopeClients: ClientStatus[]
  scopeNames: string[]
  scopeLabel: string

  activeTab: TabId
  setActiveTab: (t: TabId) => void

  settingsOpen: boolean
  setSettingsOpen: (v: boolean) => void

  /* chat sessions (client-side persisted) */
  sessions: ChatSession[]
  activeSessionId: string | null
  setActiveSessionId: (id: string | null) => void
  upsertSession: (s: ChatSession) => void
  deleteSession: (id: string) => void
}

const Ctx = createContext<Store | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [theme, setTheme] = useState<'dark' | 'light'>('dark')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  const [config, setConfig] = useState<BackendConfig | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)

  const [cores, setCores] = useState<CoreInfo[]>([])
  const [core, setCoreRaw] = useState('')
  const [clients, setClients] = useState<ClientStatus[]>([])
  const [clientsLoading, setClientsLoading] = useState(false)

  const [selectMode, setSelectModeRaw] = useState<SelectMode>('multi')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  const [activeTab, setActiveTab] = useState<TabId>('dashboard')
  const [settingsOpen, setSettingsOpen] = useState(false)

  const [sessions, setSessions] = useState<ChatSession[]>(() => loadSessions())
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])

  // Bootstrap: config + settings + cores. Only after login.
  useEffect(() => {
    if (!user) return
    let alive = true
    Promise.all([api.config(), api.cores()])
      .then(([cfg, cs]) => {
        if (!alive) return
        setConfig(cfg)
        setSettings(cfg.settings)
        setCores(cs)
        setCoreRaw((prev) => prev || cs[0]?.name || '')
      })
      .catch(() => {
        /* surfaced by views that need it */
      })
    return () => {
      alive = false
    }
  }, [user])

  const refreshClients = useCallback(() => {
    if (!core) return
    setClientsLoading(true)
    api
      .clients(core)
      .then(setClients)
      .catch(() => setClients([]))
      .finally(() => setClientsLoading(false))
  }, [core])

  // Load clients whenever the core changes.
  useEffect(() => {
    if (!core) {
      setClients([])
      return
    }
    refreshClients()
  }, [core, refreshClients])

  const setCore = useCallback((c: string) => {
    setCoreRaw(c)
    setSelectedIds(new Set())
  }, [])

  const toggleClient = useCallback(
    (name: string) => {
      setSelectedIds((prev) => {
        if (selectMode === 'single') return new Set(prev.has(name) ? [] : [name])
        const next = new Set(prev)
        if (next.has(name)) next.delete(name)
        else next.add(name)
        return next
      })
    },
    [selectMode],
  )

  const selectAll = useCallback(
    (names?: string[]) => {
      setSelectedIds(new Set(names ?? clients.map((c) => c.client)))
    },
    [clients],
  )

  const setSelectMode = useCallback((m: SelectMode) => {
    setSelectModeRaw(m)
    if (m === 'single')
      setSelectedIds((prev) => {
        const first = prev.values().next().value
        return new Set(first ? [first] : [])
      })
  }, [])

  const scopeClients = useMemo(
    () =>
      selectedIds.size === 0
        ? clients
        : clients.filter((c) => selectedIds.has(c.client)),
    [selectedIds, clients],
  )
  const scopeNames = useMemo(() => scopeClients.map((c) => c.client), [scopeClients])

  const saveSettings = useCallback(async (patch: Partial<Settings>) => {
    const next = await api.putSettings(patch)
    setSettings(next)
  }, [])

  const upsertSession = useCallback((s: ChatSession) => {
    setSessions((prev) => {
      const next = [s, ...prev.filter((x) => x.id !== s.id)].sort(
        (a, b) => b.updatedAt - a.updatedAt,
      )
      persistSessions(next)
      return next
    })
  }, [])

  const deleteSession = useCallback((id: string) => {
    setSessions((prev) => {
      const next = prev.filter((x) => x.id !== id)
      persistSessions(next)
      return next
    })
    setActiveSessionId((cur) => (cur === id ? null : cur))
  }, [])

  const store: Store = {
    user,
    login: setUser,
    logout: () => {
      setUser(null)
      setSelectedIds(new Set())
      setClients([])
      setActiveSessionId(null)
    },
    theme,
    toggleTheme: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')),
    sidebarCollapsed,
    setSidebarCollapsed,
    config,
    settings,
    saveSettings,
    cores,
    core,
    setCore,
    clients,
    clientsLoading,
    refreshClients,
    selectMode,
    setSelectMode,
    selectedIds,
    toggleClient,
    clearSelection: () => setSelectedIds(new Set()),
    selectAll,
    scopeClients,
    scopeNames,
    scopeLabel:
      selectedIds.size === 0
        ? `All in ${core || 'Core'}`
        : `${selectedIds.size} Selected`,
    activeTab,
    setActiveTab,
    settingsOpen,
    setSettingsOpen,
    sessions,
    activeSessionId,
    setActiveSessionId,
    upsertSession,
    deleteSession,
  }

  return <Ctx.Provider value={store}>{children}</Ctx.Provider>
}

export function useApp() {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useApp outside AppProvider')
  return ctx
}

export function formatMs(ms: number) {
  const m = Math.floor(ms / 60000)
  const s = Math.floor((ms % 60000) / 1000)
  const mmm = Math.floor(ms % 1000)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(mmm).padStart(3, '0')}`
}

export function formatInt(n: number) {
  return Math.round(n || 0).toLocaleString('en-US')
}
