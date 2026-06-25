import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import { loadClientStream } from '../../api'
import { FRONTEND_AGENTS } from '../../constants'
import type { ClientRun, ClientStatus, PipelineEvent, RunStatus } from '../../types'

const MAX_CONCURRENT = 3
const FRONTEND_TOTAL = FRONTEND_AGENTS.length

interface RunEntry {
  status: RunStatus
  agentsDone: number
  total: number
  currentAgent: string
  logs: string[]
  startedAt: number | null
  baseElapsedMs: number
}

function initFromStatus(c: ClientStatus): RunEntry {
  const complete = c.state === 'done'
  return {
    status: complete ? 'complete' : 'idle',
    agentsDone: complete ? c.agentsTotal : c.agentsDone,
    total: c.agentsTotal || FRONTEND_TOTAL,
    currentAgent: '',
    logs: [],
    startedAt: null,
    baseElapsedMs: 0,
  }
}

export interface Orchestrator {
  runs: Map<string, ClientRun>
  globalRunning: boolean
  runAll: () => void
  pauseAll: () => void
  toggleRow: (name: string) => void
  resetRow: (name: string) => void
  stats: { complete: number; running: number; queued: number; idle: number }
}

export function useOrchestrator(clients: ClientStatus[], core: string): Orchestrator {
  const entries = useRef<Map<string, RunEntry>>(new Map())
  const aborters = useRef<Map<string, () => void>>(new Map())
  const queue = useRef<string[]>([])
  const [, force] = useReducer((x: number) => x + 1, 0)

  const visible = useMemo(() => clients.map((c) => c.client), [clients])

  // Seed/refresh entries from incoming client status (only when not mid-run).
  useEffect(() => {
    const m = entries.current
    for (const c of clients) {
      const cur = m.get(c.client)
      if (!cur) m.set(c.client, initFromStatus(c))
      else if (cur.status === 'idle' || cur.status === 'complete') {
        // refresh idle/complete rows to reflect latest backend status
        m.set(c.client, { ...initFromStatus(c), logs: cur.logs })
      }
    }
    force()
  }, [clients])

  const promote = useCallback(() => {
    while (aborters.current.size < MAX_CONCURRENT && queue.current.length > 0) {
      const name = queue.current.shift()!
      const e = entries.current.get(name)
      if (!e || e.status !== 'queued') continue
      runOne(name)
    }
    force()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const finalize = useCallback(
    (name: string, status: RunStatus) => {
      const e = entries.current.get(name)
      if (!e) return
      if (e.startedAt != null) {
        e.baseElapsedMs += performance.now() - e.startedAt
        e.startedAt = null
      }
      e.status = status
      if (status === 'complete') e.currentAgent = ''
      aborters.current.delete(name)
      force()
      promote()
    },
    [promote],
  )

  const onEvent = useCallback((name: string, ev: PipelineEvent) => {
    const e = entries.current.get(name)
    if (!e) return
    switch (ev.type) {
      case 'pipeline_start':
        e.agentsDone = 0
        break
      case 'agent_start':
        e.currentAgent = ev.display ?? ''
        break
      case 'log':
        if (ev.message) {
          e.logs.push(ev.message)
          if (e.logs.length > 200) e.logs.splice(0, e.logs.length - 200)
        }
        break
      case 'agent_done':
        if (!ev.internal && (ev.status === 'complete' || ev.status === 'cached'))
          e.agentsDone = Math.min(e.total, e.agentsDone + 1)
        break
      case 'pipeline_done':
        // handled by onDone
        break
      case 'error':
        e.logs.push(`ERROR: ${ev.message ?? 'unknown'}`)
        e.status = 'error'
        break
    }
    force()
  }, [])

  function runOne(name: string) {
    const e = entries.current.get(name)
    if (!e) return
    e.status = 'running'
    e.startedAt = performance.now()
    e.baseElapsedMs = 0
    e.agentsDone = 0
    e.total = FRONTEND_TOTAL
    e.logs = []
    e.currentAgent = ''
    force()
    const abort = loadClientStream(
      name,
      core,
      (ev) => onEvent(name, ev),
      () => {
        const cur = entries.current.get(name)
        finalize(name, cur?.status === 'error' ? 'error' : 'complete')
      },
      () => finalize(name, 'error'),
    )
    aborters.current.set(name, abort)
  }

  const toggleRow = useCallback(
    (name: string) => {
      const e = entries.current.get(name)
      if (!e) return
      if (e.status === 'running' || e.status === 'queued') {
        // pause
        aborters.current.get(name)?.()
        aborters.current.delete(name)
        queue.current = queue.current.filter((n) => n !== name)
        if (e.startedAt != null) {
          e.baseElapsedMs += performance.now() - e.startedAt
          e.startedAt = null
        }
        e.status = 'paused'
        force()
        promote()
      } else {
        e.status = 'queued'
        queue.current.push(name)
        promote()
      }
    },
    [promote],
  )

  const resetRow = useCallback((name: string) => {
    const e = entries.current.get(name)
    if (!e) return
    aborters.current.get(name)?.()
    aborters.current.delete(name)
    queue.current = queue.current.filter((n) => n !== name)
    const status = clients.find((c) => c.client === name)
    entries.current.set(name, status ? initFromStatus(status) : {
      status: 'idle', agentsDone: 0, total: FRONTEND_TOTAL, currentAgent: '',
      logs: [], startedAt: null, baseElapsedMs: 0,
    })
    force()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clients])

  const runAll = useCallback(() => {
    for (const name of visible) {
      const e = entries.current.get(name)
      if (!e) continue
      if (e.status === 'idle' || e.status === 'paused') {
        e.status = 'queued'
        if (!queue.current.includes(name)) queue.current.push(name)
      }
    }
    promote()
  }, [visible, promote])

  const pauseAll = useCallback(() => {
    queue.current = []
    for (const name of visible) {
      const e = entries.current.get(name)
      if (!e) continue
      if (e.status === 'running' || e.status === 'queued') {
        aborters.current.get(name)?.()
        aborters.current.delete(name)
        if (e.startedAt != null) {
          e.baseElapsedMs += performance.now() - e.startedAt
          e.startedAt = null
        }
        e.status = 'paused'
      }
    }
    force()
  }, [visible])

  // Tick to refresh elapsed time while anything is running.
  const anyRunning = [...entries.current.values()].some((e) => e.startedAt != null)
  useEffect(() => {
    if (!anyRunning) return
    const id = setInterval(force, 250)
    return () => clearInterval(id)
  }, [anyRunning])

  // Abort all streams on unmount.
  useEffect(() => {
    return () => {
      for (const abort of aborters.current.values()) abort()
      aborters.current.clear()
    }
  }, [])

  // Project internal entries → exposed ClientRun (with live elapsed).
  const now = performance.now()
  const runs = useMemo(() => {
    const out = new Map<string, ClientRun>()
    for (const name of visible) {
      const e = entries.current.get(name)
      if (!e) continue
      const elapsedMs = e.baseElapsedMs + (e.startedAt != null ? now - e.startedAt : 0)
      out.set(name, {
        status: e.status,
        agentsDone: e.agentsDone,
        total: e.total,
        elapsedMs,
        currentAgent: e.currentAgent,
        logs: e.logs,
      })
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, now])

  const stats = useMemo(() => {
    const s = { complete: 0, running: 0, queued: 0, idle: 0 }
    for (const name of visible) {
      const e = entries.current.get(name)
      if (!e) continue
      if (e.status === 'complete') s.complete++
      else if (e.status === 'running') s.running++
      else if (e.status === 'queued') s.queued++
      else s.idle++ // idle, paused, error roll into "idle" bucket for the header
    }
    return s
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, now])

  const globalRunning = stats.running > 0 || stats.queued > 0

  return { runs, globalRunning, runAll, pauseAll, toggleRow, resetRow, stats }
}

export type { RunStatus }
