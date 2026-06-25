export type Role = 'admin' | 'user'

export interface User {
  name: string
  email: string
  department: string
  role: Role
}

/* ── Cores & clients (real backend shapes) ──────────────────────────── */
export interface CoreInfo {
  name: string
  clients: number
}

export type AgentKey =
  | 'contract_hierarchy'
  | 'contract_scope'
  | 'product_module'
  | 'fee_digitization'
  | 'material_match'
  | 'material_validation'
  | 'cpi_terms'
  | 'termination_clause'
  | 'mnr_template'

export interface AgentMeta {
  key: string
  display: string
}

export type ClientState = 'done' | 'partial' | 'not_run'

export interface ClientStatus {
  client: string
  agents: Record<string, boolean>
  agentsDone: number
  agentsTotal: number
  contracts: number
  state: ClientState
}

/* ── Dashboard ───────────────────────────────────────────────────────── */
export interface Portfolio {
  clients: number
  contracts: number
  items: number
  matched: number
  unmatched: number
  value: number
  pipelinePct: number
  lifecycle: { active: number; pending: number; expired: number }
}

export type RunStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'paused'
  | 'complete'
  | 'error'

/** Live pipeline run state for one client in the orchestrator. */
export interface ClientRun {
  status: RunStatus
  agentsDone: number
  total: number
  elapsedMs: number
  currentAgent: string
  logs: string[]
}

/* ── Agent outputs ───────────────────────────────────────────────────── */
export interface OutputTable {
  columns: string[]
  rows: Record<string, unknown>[]
  exists: boolean
  path?: string
  error?: string
}

/* ── Metrics ─────────────────────────────────────────────────────────── */
export interface RunRecord {
  agent: string
  display: string
  timestamp: string
  runtime_s: number
  model: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  calls: number
  status: string
  per_model?: Record<string, unknown>
}

export interface MetricsResult {
  client: string
  runs: RunRecord[]
  latestByAgent: Record<string, RunRecord>
  totals: {
    inputTokens: number
    outputTokens: number
    runtimeS: number
    runCount: number
  }
}

/* ── Chat ────────────────────────────────────────────────────────────── */
export interface ChatCitation {
  client: string
  name: string
  label: string
  page: number | null
  core: string
}

export interface ChatInvoice {
  doc: string
  url: string
}

export interface ChatResponse {
  reply: string
  citations: ChatCitation[]
  invoices: ChatInvoice[]
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: ChatCitation[]
  invoices?: ChatInvoice[]
  error?: boolean
}

/** A saved conversation (client-side persisted). */
export interface ChatSession {
  id: string
  title: string
  core: string
  focus: string[]
  messages: ChatMessage[]
  updatedAt: number
}

/* ── Settings ────────────────────────────────────────────────────────── */
export interface Settings {
  chat_model: string
  hier_model: string
  extr_model: string
  match_model: string
  cpi_model: string
  engagement_model: string
  scope_model: string
  dict_path: string
  min_year: number
}

export interface BackendConfig {
  backend: string
  settings: Settings
  models: string[]
  agents: AgentMeta[]
}

/* ── SSE pipeline events ─────────────────────────────────────────────── */
export interface PipelineEvent {
  type:
    | 'pipeline_start'
    | 'agent_start'
    | 'log'
    | 'agent_done'
    | 'pipeline_done'
    | 'error'
  key?: string
  display?: string
  internal?: boolean
  index?: number
  total?: number
  message?: string
  status?: string
  summary?: string
  agentsDone?: number
  client?: string
  elapsedMs?: number
  result?: Record<string, unknown>
}

export type TabId = 'dashboard' | 'agents' | 'chat'
