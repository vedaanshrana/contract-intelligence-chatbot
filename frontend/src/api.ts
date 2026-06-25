/**
 * Typed client for the Contract Intelligence API (../Contract Chatbot/server.py).
 *
 * All paths are relative ("/api/..."). In dev, Vite proxies /api → :8000
 * (see vite.config.ts); in prod the SPA is served from the same origin as the
 * API, so relative paths just work.
 */
import type {
  BackendConfig,
  ChatResponse,
  ClientStatus,
  CoreInfo,
  MetricsResult,
  OutputTable,
  PipelineEvent,
  Settings,
} from './types'

const BASE = ''

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json())?.detail ?? ''
    } catch {
      detail = await res.text().catch(() => '')
    }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

const enc = encodeURIComponent

export const api = {
  health: () => j<{ ok: boolean }>('/api/health'),
  config: () => j<BackendConfig>('/api/config'),

  getSettings: () => j<Settings>('/api/settings'),
  putSettings: (patch: Partial<Settings>) =>
    j<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  cores: () => j<CoreInfo[]>('/api/cores'),
  clients: (core: string, status = true) =>
    j<ClientStatus[]>(`/api/clients?core=${enc(core)}&status=${status}`),
  clientStatus: (client: string) =>
    j<ClientStatus>(`/api/clients/${enc(client)}/status`),
  metrics: (client: string) =>
    j<MetricsResult>(`/api/clients/${enc(client)}/metrics`),

  portfolio: (clients: string[]) =>
    j<import('./types').Portfolio>(
      `/api/portfolio?clients=${enc(clients.join(','))}`,
    ),

  pdfs: (client: string, core = '') =>
    j<{ core: string; pdfs: { name: string; label: string }[] }>(
      `/api/clients/${enc(client)}/pdfs?core=${enc(core)}`,
    ),
  pdfUrl: (client: string, name: string, core = '', page?: number | null) => {
    const frag = page ? `#page=${page}` : ''
    return `/api/clients/${enc(client)}/pdfs/${enc(name)}?core=${enc(core)}${frag}`
  },

  output: (client: string, key: string) =>
    j<OutputTable>(`/api/clients/${enc(client)}/outputs/${enc(key)}`),
  exportUrl: (client: string, key: string) =>
    `/api/clients/${enc(client)}/outputs/${enc(key)}/export`,

  /** URL of the Hierarchy agent's interactive Plotly graph, themed to match the
   *  app (theme = 'dark' | 'light'). Embed in an <iframe> to keep interactivity. */
  hierarchyHtmlUrl: (client: string, theme: 'dark' | 'light' = 'dark') =>
    `/api/clients/${enc(client)}/hierarchy/html?theme=${enc(theme)}`,

  chat: (focus: string[], messages: { role: string; content: string }[], snowflake: boolean) =>
    j<ChatResponse>('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ focus, messages, snowflake }),
    }),

  snowflakeStatus: () => j<Record<string, unknown>>('/api/snowflake/status'),

  submitFeedback: (payload: {
    category: string
    title: string
    description: string
    question: string
    answer: string
    core: string
    clients: string[]
    chat_model: string
    user_name: string
    user_email: string
  }) =>
    j<{ saved: boolean; path: string; count: number }>('/api/feedback', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  dictionary: (core: string) =>
    j<{
      core: string
      override: string
      resolved: string
      resolvedName: string
      matchingEnabled: boolean
    }>(`/api/cores/${enc(core)}/dictionary`),

  uploadDictionary: async (core: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`${BASE}/api/cores/${enc(core)}/dictionary`, {
      method: 'POST',
      body: form,
    })
    if (!res.ok) throw new Error(await res.text())
    return res.json() as Promise<{ saved: string; name: string }>
  },
}

/**
 * POST to an SSE endpoint and invoke `onEvent` for each `data:` event.
 * Returns a function that aborts the stream.
 */
export function streamPipeline(
  path: string,
  body: Record<string, unknown> | null,
  onEvent: (ev: PipelineEvent) => void,
  onDone?: () => void,
  onError?: (e: Error) => void,
): () => void {
  const ctrl = new AbortController()
  ;(async () => {
    try {
      const res = await fetch(`${BASE}${path}`, {
        method: 'POST',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      })
      if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const chunks = buf.split('\n\n')
        buf = chunks.pop() ?? ''
        for (const chunk of chunks) {
          const line = chunk.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          const payload = line.slice(5).trim()
          if (!payload) continue
          try {
            onEvent(JSON.parse(payload) as PipelineEvent)
          } catch {
            /* ignore malformed event */
          }
        }
      }
      onDone?.()
    } catch (e) {
      if ((e as Error).name !== 'AbortError') onError?.(e as Error)
      else onDone?.()
    }
  })()
  return () => ctrl.abort()
}

export function loadClientStream(
  client: string,
  core: string,
  onEvent: (ev: PipelineEvent) => void,
  onDone?: () => void,
  onError?: (e: Error) => void,
) {
  return streamPipeline(
    `/api/clients/${enc(client)}/load?core=${enc(core)}`,
    null,
    onEvent,
    onDone,
    onError,
  )
}

export function runAgentStream(
  key: string,
  client: string,
  core: string,
  onEvent: (ev: PipelineEvent) => void,
  onDone?: () => void,
  onError?: (e: Error) => void,
) {
  return streamPipeline(
    `/api/agents/${enc(key)}/run`,
    { client, core },
    onEvent,
    onDone,
    onError,
  )
}
