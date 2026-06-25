import { useEffect, useMemo, useState } from 'react'
import { Activity, ChevronDown, ChevronRight, Download, ExternalLink, GitBranch, Search } from 'lucide-react'
import { formatInt, useApp } from '../../store'
import { api } from '../../api'
import { FRONTEND_AGENTS } from '../../constants'
import type { ClientStatus, MetricsResult, OutputTable } from '../../types'
import Dropdown from '../ui/Dropdown'
import EntityGraph from './EntityGraph'
import MetricsDrawer from './MetricsDrawer'

const CARD_LIMIT = 6
const ROW_CAP = 500

/* ── Generic output grid (dynamic columns) ───────────────────────── */
function DataGrid({ table, query }: { table: OutputTable; query: string }) {
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const base = q
      ? table.rows.filter((r) =>
          Object.values(r).some((v) => String(v ?? '').toLowerCase().includes(q)),
        )
      : table.rows
    return base
  }, [table.rows, query])

  if (!table.exists) {
    return (
      <div className="flex h-[300px] items-center justify-center border-b border-line text-center text-[11px] text-ink-3">
        Not run yet — run this agent from the Dashboard orchestrator or the agent
        controls.
      </div>
    )
  }
  if (table.error) {
    return (
      <div className="flex h-[300px] items-center justify-center border-b border-line px-6 text-center text-[11px] text-bad">
        Could not read output: {table.error}
      </div>
    )
  }
  if (table.rows.length === 0) {
    return (
      <div className="flex h-[300px] items-center justify-center border-b border-line text-center text-[11px] text-ink-3">
        Agent ran but produced no rows.
      </div>
    )
  }

  const shown = rows.slice(0, ROW_CAP)
  return (
    <div className="h-[300px] overflow-auto border-b border-line">
      <table className="w-max min-w-full border-separate border-spacing-0">
        <thead>
          <tr>
            {table.columns.map((h, i) => (
              <th
                key={h}
                className={`sticky top-0 z-10 whitespace-nowrap border-b border-r border-line bg-surface-2 px-3 py-1.5 text-left font-mono text-[8.5px] uppercase tracking-[0.12em] text-ink-3 ${
                  i === 0 ? 'left-0 z-20' : ''
                }`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, ri) => (
            <tr key={ri} className="group/row">
              {table.columns.map((c, ci) => {
                const val = r[c]
                const text = val == null ? '' : String(val)
                return (
                  <td
                    key={c}
                    className={`whitespace-nowrap border-b border-r border-line/60 px-3 py-[5px] font-mono text-[10.5px] transition-colors duration-150 ${
                      ci === 0
                        ? 'sticky left-0 z-10 bg-surface font-medium text-ink group-hover/row:bg-surface-2'
                        : 'text-ink-2 group-hover/row:bg-surface-2/50'
                    }`}
                    title={text.length > 60 ? text : undefined}
                  >
                    {text.length > 80 ? text.slice(0, 78) + '…' : text}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > ROW_CAP && (
        <div className="bg-surface-2/60 px-3 py-1 text-center font-mono text-[9px] text-ink-3">
          showing first {ROW_CAP} of {formatInt(rows.length)} rows — export for the full set
        </div>
      )}
    </div>
  )
}

/* ── Interactive Plotly hierarchy graph (served & themed by the backend) ── */
function HierarchyHtml({
  client,
  theme,
  exists,
}: {
  client: string
  theme: 'dark' | 'light'
  exists: boolean
}) {
  if (!exists) {
    return (
      <div className="flex h-[320px] items-center justify-center bg-bg/40 px-6 text-center text-[11px] text-ink-3">
        Run the Hierarchy agent to generate the interactive contract graph.
      </div>
    )
  }
  return (
    <iframe
      // re-key on theme so switching light/dark reloads the re-themed graph
      key={`${client}-${theme}`}
      src={api.hierarchyHtmlUrl(client, theme)}
      title={`${client} — interactive contract hierarchy`}
      className="h-[600px] w-full border-0 bg-bg"
    />
  )
}

/* ── Per-client card ─────────────────────────────────────────────── */
function ClientCard({ client, onMetrics }: { client: ClientStatus; onMetrics: () => void }) {
  const { theme } = useApp()
  const [agentKey, setAgentKey] = useState('fee_digitization')
  const [table, setTable] = useState<OutputTable | null>(null)
  const [hier, setHier] = useState<OutputTable | null>(null)
  const [showHier, setShowHier] = useState(false)
  const [hierView, setHierView] = useState<'interactive' | 'quick'>('interactive')
  const [metrics, setMetrics] = useState<MetricsResult | null>(null)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let alive = true
    setLoading(true)
    api
      .output(client.client, agentKey)
      .then((t) => alive && setTable(t))
      .catch(() => alive && setTable(null))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [client.client, agentKey])

  useEffect(() => {
    let alive = true
    api.output(client.client, 'contract_hierarchy').then((t) => alive && setHier(t)).catch(() => {})
    api.metrics(client.client).then((m) => alive && setMetrics(m)).catch(() => {})
    return () => {
      alive = false
    }
  }, [client.client])

  const totals = metrics?.totals
  const agentMeta = FRONTEND_AGENTS.find((a) => a.key === agentKey)
  const agentRun =
    agentMeta && metrics ? metrics.latestByAgent[agentMeta.metricKey] : undefined

  return (
    <div className="min-w-0 border border-line bg-surface">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className="text-[13px] font-semibold text-ink">{client.client}</span>
          <span
            className={`px-1.5 py-[2px] font-mono text-[8.5px] uppercase tracking-wider ${
              client.state === 'done'
                ? 'bg-ok-dim text-ok'
                : client.state === 'partial'
                  ? 'bg-warn-dim text-warn'
                  : 'bg-surface-2 text-ink-3'
            }`}
          >
            {client.agentsDone}/{client.agentsTotal} agents
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="flex items-center gap-1.5 border border-line bg-surface-2 px-2 py-1">
            <Search size={11} className="text-ink-3" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter rows…"
              className="w-28 bg-transparent text-[11px] text-ink outline-none placeholder:text-ink-3"
            />
          </div>
          <button
            onClick={onMetrics}
            className="flex items-center gap-1.5 border border-line px-2.5 py-1 font-mono text-[9.5px] uppercase tracking-wider text-ink-2 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
          >
            <Activity size={11} />
            Metrics
          </button>
          <a
            href={table?.exists ? api.exportUrl(client.client, agentKey) : undefined}
            className={`flex items-center gap-1.5 px-2.5 py-1 font-mono text-[9.5px] uppercase tracking-wider transition-all duration-150 ${
              table?.exists
                ? 'bg-primary text-white hover:brightness-110'
                : 'cursor-not-allowed bg-surface-2 text-ink-3'
            }`}
          >
            <Download size={11} />
            Export .xlsx
          </a>
        </div>
      </div>

      {/* token & performance ticker (real metrics) */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line bg-surface-2/50 px-4 py-1.5 font-mono text-[10px]">
        <span className="text-ink-3">
          Input Tokens: <span className="text-ink">{formatInt(totals?.inputTokens ?? 0)}</span>
        </span>
        <span className="text-line-strong">|</span>
        <span className="text-ink-3">
          Output Tokens: <span className="text-ink">{formatInt(totals?.outputTokens ?? 0)}</span>
        </span>
        <span className="text-line-strong">|</span>
        <span className="text-ink-3">
          Total Runtime: <span className="text-primary">{formatInt(totals?.runtimeS ?? 0)}s</span>
        </span>
        <span className="text-line-strong">|</span>
        <span className="text-ink-3">
          Runs: <span className="text-ink">{formatInt(totals?.runCount ?? 0)}</span>
        </span>
      </div>

      {/* per-agent metrics for the currently selected agent tab */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line px-4 py-1.5 font-mono text-[10px]">
        <span className="uppercase tracking-wider text-ink-2">
          {(agentMeta?.display ?? 'Agent').replace(' Agent', '')}
        </span>
        {agentRun ? (
          <>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">
              Input: <span className="text-ink">{formatInt(agentRun.input_tokens)}</span>
            </span>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">
              Output: <span className="text-ink">{formatInt(agentRun.output_tokens)}</span>
            </span>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">
              Runtime: <span className="text-primary">{agentRun.runtime_s}s</span>
            </span>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">
              API Calls: <span className="text-ink">{formatInt(agentRun.calls)}</span>
            </span>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">
              Model: <span className="text-ink">{agentRun.model || '—'}</span>
            </span>
          </>
        ) : (
          <>
            <span className="text-line-strong">|</span>
            <span className="text-ink-3">No run recorded for this agent yet</span>
          </>
        )}
      </div>

      {/* agent tab bar */}
      <div className="flex flex-wrap gap-px overflow-x-auto border-b border-line bg-surface-2/30 px-2 py-1.5">
        {FRONTEND_AGENTS.map((a) => {
          const done = client.agents[a.key]
          const active = agentKey === a.key
          return (
            <button
              key={a.key}
              onClick={() => setAgentKey(a.key)}
              className={`flex items-center gap-1.5 whitespace-nowrap px-2.5 py-1 text-[10.5px] transition-colors duration-150 ${
                active ? 'bg-surface font-medium text-ink shadow-sm' : 'text-ink-3 hover:text-ink-2'
              }`}
              title={a.blurb}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${done ? 'bg-ok' : 'bg-line-strong'}`} />
              {a.display.replace(' Agent', '')}
            </button>
          )
        })}
      </div>

      {/* data grid */}
      {loading && !table ? (
        <div className="flex h-[300px] items-center justify-center border-b border-line font-mono text-[10px] uppercase tracking-wider text-ink-3">
          Loading…
        </div>
      ) : (
        <DataGrid table={table ?? { columns: [], rows: [], exists: false }} query={query} />
      )}

      {/* contract hierarchy — collapsible, independent of the selected agent */}
      <div>
        <button
          onClick={() => setShowHier((v) => !v)}
          className="flex w-full items-center justify-between gap-2 bg-surface-2/30 px-4 py-2 text-left transition-colors duration-150 hover:bg-surface-2/60"
          aria-expanded={showHier}
        >
          <span className="flex items-center gap-2 font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
            <GitBranch size={11} className={showHier ? 'text-primary' : ''} />
            Contract Hierarchy
          </span>
          {showHier ? (
            <ChevronDown size={13} className="text-ink-3" />
          ) : (
            <ChevronRight size={13} className="text-ink-3" />
          )}
        </button>
        {showHier && (
          <div className="border-t border-line">
            {/* view switcher: the backend's full interactive Plotly graph, or
                the lightweight inline SVG */}
            <div className="flex items-center justify-between gap-2 border-b border-line bg-surface-2/30 px-3 py-1.5">
              <div className="flex border border-line p-[2px]">
                {(
                  [
                    ['interactive', 'Interactive'],
                    ['quick', 'Quick'],
                  ] as const
                ).map(([m, label]) => (
                  <button
                    key={m}
                    onClick={() => setHierView(m)}
                    className={`px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider transition-colors duration-150 ${
                      hierView === m ? 'bg-primary-dim text-primary' : 'text-ink-3 hover:text-ink-2'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {hierView === 'interactive' && hier?.exists && (
                <a
                  href={api.hierarchyHtmlUrl(client.client, theme)}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-wider text-ink-3 transition-colors duration-150 hover:text-primary"
                  title="Open the full interactive graph in a new tab"
                >
                  <ExternalLink size={11} /> Open full
                </a>
              )}
            </div>
            {hierView === 'interactive' ? (
              <HierarchyHtml client={client.client} theme={theme} exists={!!hier?.exists} />
            ) : (
              <div className="h-[300px]">
                <EntityGraph rows={hier?.rows ?? []} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default function AgentsView() {
  const { scopeClients } = useApp()
  const [clientFilter, setClientFilter] = useState('all')
  const [metricsClient, setMetricsClient] = useState<string | null>(null)

  const visible = useMemo(() => {
    const base =
      clientFilter === 'all'
        ? scopeClients
        : scopeClients.filter((c) => c.client === clientFilter)
    return base.slice(0, CARD_LIMIT)
  }, [scopeClients, clientFilter])

  const totalInScope = clientFilter === 'all' ? scopeClients.length : 1

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="sticky top-0 z-30 flex shrink-0 flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-bg/95 px-4 py-2.5 backdrop-blur">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">Client</span>
          <div className="w-64">
            <Dropdown
              searchable
              value={clientFilter}
              onChange={setClientFilter}
              options={[
                { value: 'all', label: `All in scope (${scopeClients.length})` },
                ...scopeClients.map((c) => ({
                  value: c.client,
                  label: c.client,
                  hint: `${c.agentsDone}/${c.agentsTotal}`,
                })),
              ]}
            />
          </div>
        </div>

        <span className="ml-auto font-mono text-[9.5px] text-ink-3">
          Showing {visible.length} of {totalInScope} client outputs
        </span>
      </div>

      <div className="min-h-0 min-w-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden p-4">
        {visible.map((c) => (
          <ClientCard key={c.client} client={c} onMetrics={() => setMetricsClient(c.client)} />
        ))}
        {visible.length === 0 && (
          <div className="flex h-40 items-center justify-center border border-dashed border-line text-[12px] text-ink-3">
            No clients in scope — select clients from the navigator
          </div>
        )}
      </div>

      <MetricsDrawer client={metricsClient} onClose={() => setMetricsClient(null)} />
    </div>
  )
}
