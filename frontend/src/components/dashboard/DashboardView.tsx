import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  FileStack,
  Pause,
  Play,
  RotateCcw,
  ScanBarcode,
} from 'lucide-react'
import { formatInt, formatMs, useApp } from '../../store'
import { api } from '../../api'
import type { ClientRun, ClientStatus, Portfolio, RunStatus } from '../../types'
import { useOrchestrator } from './useOrchestrator'
import AgentRunPanel from './AgentRunPanel'

/* ── KPI cards ──────────────────────────────────────────────────── */
function KpiShell({
  label,
  icon: Icon,
  children,
}: {
  label: string
  icon: React.ElementType
  children: React.ReactNode
}) {
  const { scopeLabel } = useApp()
  return (
    <div className="flex flex-col border border-line bg-surface p-4 transition-colors duration-150 hover:border-line-strong">
      <div className="flex items-center justify-between pb-3">
        <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-ink-3">{label}</span>
        <Icon size={13} className="text-ink-3" />
      </div>
      <div className="flex-1">{children}</div>
      <div className="pt-3 font-mono text-[9px] uppercase tracking-wider text-ink-3/70">
        Scope · {scopeLabel}
      </div>
    </div>
  )
}

function LifecycleBar({ lifecycle }: { lifecycle: Portfolio['lifecycle'] }) {
  const sum = lifecycle.active + lifecycle.pending + lifecycle.expired || 1
  const seg = [
    { key: 'Active', val: lifecycle.active, cls: 'bg-ok' },
    { key: 'Root / partial', val: lifecycle.pending, cls: 'bg-warn' },
    { key: 'Superseded', val: lifecycle.expired, cls: 'bg-ink-3' },
  ]
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden bg-surface-3">
        {seg.map((s) => (
          <div key={s.key} className={`${s.cls} transition-all duration-150`} style={{ width: `${(s.val / sum) * 100}%` }} />
        ))}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        {seg.map((s) => (
          <div key={s.key}>
            <div className="flex items-center gap-1.5">
              <span className={`h-1.5 w-1.5 ${s.cls}`} />
              <span className="text-[10px] text-ink-3">{s.key}</span>
            </div>
            <div className="font-mono text-[15px] font-semibold text-ink">{formatInt(s.val)}</div>
            <div className="font-mono text-[9px] text-ink-3">{((s.val / sum) * 100).toFixed(1)}%</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function KpiGrid({ p }: { p: Portfolio }) {
  const matchPct = (p.matched / (p.matched + p.unmatched || 1)) * 100
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <KpiShell label="Clients in Scope" icon={Building2}>
        <div className="font-mono text-[34px] font-semibold leading-none tracking-tight text-ink">
          {formatInt(p.clients)}
        </div>
        <div className="mt-2 text-[11px] text-ink-3">entities in active analysis scope</div>
      </KpiShell>

      <KpiShell label="Contracts Analyzed" icon={FileStack}>
        <div className="font-mono text-[34px] font-semibold leading-none tracking-tight text-ink">
          {formatInt(p.contracts)}
        </div>
        <div className="mt-2 text-[11px] text-ink-3">
          {formatInt(p.items)} fee line items · {p.pipelinePct}% pipeline complete
          {p.value ? ` · $${formatInt(p.value)} extracted` : ''}
        </div>
      </KpiShell>

      <KpiShell label="Contract Lifecycle" icon={FileStack}>
        <LifecycleBar lifecycle={p.lifecycle} />
      </KpiShell>

      <KpiShell label="Material Code Status" icon={ScanBarcode}>
        <div className="flex items-end gap-5">
          <div>
            <div className="font-mono text-[22px] font-semibold leading-none text-ok">{formatInt(p.matched)}</div>
            <div className="mt-1 text-[10px] text-ink-3">Matched</div>
          </div>
          <div>
            <div className="font-mono text-[22px] font-semibold leading-none text-bad">{formatInt(p.unmatched)}</div>
            <div className="mt-1 text-[10px] text-ink-3">Unmatched</div>
          </div>
          <div className="ml-auto text-right">
            <div className="font-mono text-[22px] font-semibold leading-none text-ink">{matchPct.toFixed(1)}%</div>
            <div className="mt-1 text-[10px] text-ink-3">Match rate</div>
          </div>
        </div>
        <div className="mt-3 flex h-2.5 w-full overflow-hidden bg-surface-3">
          <div className="bg-ok transition-all duration-150" style={{ width: `${matchPct}%` }} />
          <div className="bg-bad/70 transition-all duration-150" style={{ width: `${100 - matchPct}%` }} />
        </div>
      </KpiShell>
    </div>
  )
}

/* ── Status tag ─────────────────────────────────────────────────── */
function StatusTag({ status }: { status: RunStatus }) {
  switch (status) {
    case 'complete':
      return (
        <span className="inline-flex items-center gap-1.5 bg-ok-dim px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-ok">
          <Check size={10} strokeWidth={3} /> Complete
        </span>
      )
    case 'running':
      return (
        <span className="inline-flex items-center gap-1.5 bg-primary-dim px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-primary">
          <span className="animate-arc inline-block h-[10px] w-[10px] rounded-full border-[1.5px] border-primary/25 border-t-primary" />
          Running
        </span>
      )
    case 'queued':
      return (
        <span className="animate-status-pulse inline-flex items-center gap-1.5 border border-dashed border-line-strong px-2 py-[2px] font-mono text-[9px] uppercase tracking-wider text-ink-2">
          <span className="h-1.5 w-1.5 rounded-full border border-ink-3" /> Queued
        </span>
      )
    case 'paused':
      return (
        <span className="inline-flex items-center gap-1.5 bg-warn-dim px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-warn">
          <Pause size={9} strokeWidth={3} /> Paused
        </span>
      )
    case 'error':
      return (
        <span className="inline-flex items-center gap-1.5 bg-bad-dim px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-bad">
          <AlertCircle size={10} /> Error
        </span>
      )
    default:
      return (
        <span className="inline-flex items-center gap-1.5 bg-surface-2 px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-ink-3">
          <span className="h-1.5 w-1.5 rounded-full bg-ink-3" /> Idle
        </span>
      )
  }
}

/* ── Orchestrator row ───────────────────────────────────────────── */
function OrchRow({
  client,
  run,
  expanded,
  onExpand,
  onToggle,
  onReset,
}: {
  client: ClientStatus
  run: ClientRun
  expanded: boolean
  onExpand: () => void
  onToggle: () => void
  onReset: () => void
}) {
  const pct = (run.agentsDone / (run.total || 9)) * 100
  const active = run.status === 'running'
  return (
    <tr className="group border-b border-line/60 transition-colors duration-150 hover:bg-surface-2/40">
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2">
          <button
            onClick={onExpand}
            className="flex h-5 w-5 shrink-0 items-center justify-center border border-line text-ink-3 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
            aria-label={expanded ? 'Hide agents' : 'Show agents'}
            title={expanded ? 'Hide individual agents' : 'Run individual agents'}
          >
            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
          <div className="min-w-0">
            <div className="text-[12px] font-medium text-ink">{client.client}</div>
            <div className="font-mono text-[9px] text-ink-3">
              {active && run.currentAgent ? `▸ ${run.currentAgent}` : `${client.contracts} contracts`}
            </div>
          </div>
        </div>
      </td>
      <td className="px-4 py-2.5">
        <StatusTag status={run.status} />
      </td>
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className="w-[88px] font-mono text-[10.5px] text-ink-2">
            {run.agentsDone}/{run.total} Agents
          </span>
          <div className="h-[4px] w-28 bg-surface-3">
            <div
              className={`h-full transition-all duration-150 ${run.status === 'complete' ? 'bg-ok' : run.status === 'error' ? 'bg-bad' : 'bg-primary'}`}
              style={{ width: `${Math.min(100, pct)}%` }}
            />
          </div>
        </div>
      </td>
      <td className="px-4 py-2.5">
        <span
          className={`font-mono text-[11px] tabular-nums ${
            active ? 'text-primary' : run.status === 'complete' ? 'text-ink' : 'text-ink-3'
          }`}
        >
          {run.elapsedMs > 0 || active ? formatMs(run.elapsedMs) : '—'}
        </span>
      </td>
      <td className="px-4 py-2.5 text-right">
        <div className="flex items-center justify-end gap-1">
          <button
            onClick={onToggle}
            className={`flex h-6 w-6 items-center justify-center border transition-colors duration-150 ${
              active || run.status === 'queued'
                ? 'border-warn/40 text-warn hover:bg-warn-dim'
                : 'border-line text-ink-3 hover:border-primary/50 hover:text-primary'
            }`}
            aria-label={active ? 'Pause pipeline' : 'Run pipeline'}
          >
            {active || run.status === 'queued' ? <Pause size={11} /> : <Play size={11} />}
          </button>
          <button
            onClick={onReset}
            className="flex h-6 w-6 items-center justify-center border border-line text-ink-3 opacity-0 transition-all duration-150 hover:text-ink group-hover:opacity-100"
            aria-label="Reset pipeline"
          >
            <RotateCcw size={11} />
          </button>
        </div>
      </td>
    </tr>
  )
}

/* ── Master orchestrator canvas ─────────────────────────────────── */
function Orchestrator({
  clients,
  core,
  onAnyComplete,
}: {
  clients: ClientStatus[]
  core: string
  onAnyComplete: () => void
}) {
  const orch = useOrchestrator(clients, core)
  const { globalRunning, stats } = orch

  // Which client rows have their individual-agent panel expanded.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggleExpand = useCallback((name: string) => {
    setExpanded((prev) => {
      const n = new Set(prev)
      n.has(name) ? n.delete(name) : n.add(name)
      return n
    })
  }, [])

  // When all runs settle (nothing running/queued), refresh portfolio KPIs.
  const prevRunning = useRef(globalRunning)
  useEffect(() => {
    if (prevRunning.current && !globalRunning) onAnyComplete()
    prevRunning.current = globalRunning
  }, [globalRunning, onAnyComplete])

  return (
    <div className="flex min-h-0 flex-1 flex-col border border-line bg-surface">
      <div className="flex shrink-0 items-center justify-between border-b border-line bg-surface-2/70 px-4 py-2.5">
        <div className="flex items-center gap-3">
          <span className="text-[12.5px] font-semibold text-ink">Master Orchestrator</span>
          <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
            {clients.length} pipelines
          </span>
        </div>
        <div className="flex items-center gap-4">
          <div className="hidden items-center gap-3 font-mono text-[9.5px] uppercase tracking-wider md:flex">
            <span className="text-ok">{stats.complete} done</span>
            <span className="text-primary">{stats.running} running</span>
            <span className="text-ink-2">{stats.queued} queued</span>
            <span className="text-ink-3">{stats.idle} idle</span>
          </div>
          <button
            onClick={globalRunning ? orch.pauseAll : orch.runAll}
            className={`flex items-center gap-2 px-4 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-white transition-colors duration-150 ${
              globalRunning ? 'bg-warn hover:brightness-110' : 'bg-ok hover:brightness-110'
            }`}
          >
            {globalRunning ? (
              <>
                <Pause size={11} strokeWidth={3} /> Pause All
              </>
            ) : (
              <>
                <Play size={11} strokeWidth={3} /> Run All
              </>
            )}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full">
          <thead className="sticky top-0 z-10 bg-surface">
            <tr className="border-b border-line text-left font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
              <th className="px-4 py-2 font-medium">Client Pipeline</th>
              <th className="w-32 px-4 py-2 font-medium">Status</th>
              <th className="w-60 px-4 py-2 font-medium">Progress</th>
              <th className="w-32 px-4 py-2 font-medium">Runtime</th>
              <th className="w-24 px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {clients.map((c) => {
              const run = orch.runs.get(c.client)
              if (!run) return null
              const isOpen = expanded.has(c.client)
              const locked = run.status === 'running' || run.status === 'queued'
              return (
                <Fragment key={c.client}>
                  <OrchRow
                    client={c}
                    run={run}
                    expanded={isOpen}
                    onExpand={() => toggleExpand(c.client)}
                    onToggle={() => orch.toggleRow(c.client)}
                    onReset={() => orch.resetRow(c.client)}
                  />
                  {isOpen && (
                    <tr className="border-b border-line/60 bg-surface-2/20">
                      <td colSpan={5} className="px-4 py-3">
                        <AgentRunPanel
                          client={c}
                          core={core}
                          disabled={locked}
                          onComplete={onAnyComplete}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {clients.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-[12px] text-ink-3">
                  No clients in scope — select clients from the navigator
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function DashboardView() {
  const { scopeClients, scopeNames, core, refreshClients } = useApp()
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)

  const loadPortfolio = useCallback(() => {
    if (scopeNames.length === 0) {
      setPortfolio({
        clients: 0, contracts: 0, items: 0, matched: 0, unmatched: 0, value: 0,
        pipelinePct: 0, lifecycle: { active: 0, pending: 0, expired: 0 },
      })
      return
    }
    api.portfolio(scopeNames).then(setPortfolio).catch(() => setPortfolio(null))
  }, [scopeNames.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    loadPortfolio()
  }, [loadPortfolio])

  const onAnyComplete = useCallback(() => {
    refreshClients()
    loadPortfolio()
  }, [refreshClients, loadPortfolio])

  const pipelineClients = useMemo(() => scopeClients.slice(0, 40), [scopeClients])

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4 xl:overflow-hidden">
      {portfolio && <KpiGrid p={portfolio} />}
      <Orchestrator clients={pipelineClients} core={core} onAnyComplete={onAnyComplete} />
    </div>
  )
}
