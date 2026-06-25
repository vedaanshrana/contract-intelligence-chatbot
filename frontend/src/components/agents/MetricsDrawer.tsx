import { useEffect, useState } from 'react'
import { Activity, X } from 'lucide-react'
import { api } from '../../api'
import { formatInt } from '../../store'
import type { MetricsResult, RunRecord } from '../../types'

function StatusDot({ status }: { status: string }) {
  const ok = status === 'complete'
  return (
    <span
      className={`inline-block h-1.5 w-1.5 rounded-full ${ok ? 'bg-ok' : 'bg-bad'}`}
      title={status}
    />
  )
}

function TokenBars({ runs }: { runs: RunRecord[] }) {
  const recent = runs.slice(-12)
  const max = Math.max(1, ...recent.map((r) => r.total_tokens))
  const W = 420
  const H = 110
  const bw = recent.length ? W / recent.length : W
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      {[0.25, 0.5, 0.75].map((f) => (
        <line key={f} x1="0" x2={W} y1={H * f} y2={H * f} stroke="var(--line)" strokeWidth="1" />
      ))}
      {recent.map((r, i) => {
        const h = (r.total_tokens / max) * (H - 10)
        return (
          <rect
            key={i}
            x={i * bw + bw * 0.15}
            y={H - h}
            width={bw * 0.7}
            height={h}
            fill={r.status === 'complete' ? 'var(--primary)' : 'var(--bad)'}
            opacity={0.85}
          >
            <title>{`${r.display}: ${formatInt(r.total_tokens)} tokens`}</title>
          </rect>
        )
      })}
    </svg>
  )
}

export default function MetricsDrawer({
  client,
  onClose,
}: {
  client: string | null
  onClose: () => void
}) {
  const [data, setData] = useState<MetricsResult | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!client) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [client, onClose])

  useEffect(() => {
    if (!client) {
      setData(null)
      return
    }
    let alive = true
    setLoading(true)
    api
      .metrics(client)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [client])

  const runs = data?.runs ?? []

  return (
    <>
      <div
        onClick={onClose}
        className={`fixed inset-0 z-90 bg-black/30 backdrop-blur-[2px] transition-opacity duration-150 ${
          client ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
      />
      <div
        className={`fixed right-0 top-0 z-95 flex h-full w-[480px] max-w-[92vw] flex-col border-l border-line-strong bg-surface shadow-2xl shadow-black/40 transition-transform duration-150 ${
          client ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        {client && (
          <>
            <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-3">
              <div>
                <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
                  <Activity size={14} className="text-primary" />
                  Execution Metrics
                </div>
                <div className="truncate font-mono text-[9px] uppercase tracking-[0.18em] text-ink-3">
                  {client} · {runs.length} runs
                </div>
              </div>
              <button
                onClick={onClose}
                className="flex h-7 w-7 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
                aria-label="Close metrics"
              >
                <X size={15} />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              {loading && (
                <div className="py-10 text-center font-mono text-[10px] uppercase tracking-wider text-ink-3">
                  Loading metrics…
                </div>
              )}
              {!loading && data && (
                <>
                  <div className="grid grid-cols-4 gap-2">
                    {[
                      ['Total Runs', formatInt(data.totals.runCount)],
                      ['Runtime', `${formatInt(data.totals.runtimeS)}s`],
                      ['Input Tok', formatInt(data.totals.inputTokens)],
                      ['Output Tok', formatInt(data.totals.outputTokens)],
                    ].map(([k, v]) => (
                      <div key={k} className="border border-line bg-surface-2/60 p-2.5">
                        <div className="font-mono text-[8.5px] uppercase tracking-wider text-ink-3">{k}</div>
                        <div className="mt-1 font-mono text-[13px] font-semibold text-ink">{v}</div>
                      </div>
                    ))}
                  </div>

                  {runs.length > 0 && (
                    <div className="mt-4 border border-line">
                      <div className="flex items-center justify-between border-b border-line bg-surface-2/60 px-3 py-1.5">
                        <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
                          Tokens / run (last 12)
                        </span>
                      </div>
                      <div className="p-3">
                        <TokenBars runs={runs} />
                      </div>
                    </div>
                  )}

                  {/* latest per-agent table — per-agent input/output tokens,
                      runtime, API calls and the model actually used */}
                  <div className="mt-4 border border-line">
                    <div className="border-b border-line bg-surface-2/60 px-3 py-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
                      Per-agent metrics (latest run)
                    </div>
                    <div className="max-h-72 overflow-auto">
                      <table className="w-full">
                        <thead className="sticky top-0 bg-surface">
                          <tr className="border-b border-line text-left font-mono text-[8.5px] uppercase tracking-wider text-ink-3">
                            <th className="px-2.5 py-1.5 font-medium">Agent / Model</th>
                            <th className="px-1.5 py-1.5 text-right font-medium">In</th>
                            <th className="px-1.5 py-1.5 text-right font-medium">Out</th>
                            <th className="px-1.5 py-1.5 text-right font-medium">Calls</th>
                            <th className="px-1.5 py-1.5 text-right font-medium">Runtime</th>
                            <th className="px-2.5 py-1.5 font-medium"> </th>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.values(data.latestByAgent).map((r) => (
                            <tr key={r.agent} className="border-b border-line/60">
                              <td className="px-2.5 py-1.5">
                                <div className="text-[11px] text-ink">{r.display || r.agent}</div>
                                <div className="font-mono text-[9px] text-ink-3">{r.model || '—'}</div>
                              </td>
                              <td className="px-1.5 py-1.5 text-right font-mono text-[10px] tabular-nums text-ink-2">
                                {formatInt(r.input_tokens)}
                              </td>
                              <td className="px-1.5 py-1.5 text-right font-mono text-[10px] tabular-nums text-ink-2">
                                {formatInt(r.output_tokens)}
                              </td>
                              <td className="px-1.5 py-1.5 text-right font-mono text-[10px] tabular-nums text-ink-2">
                                {formatInt(r.calls)}
                              </td>
                              <td className="px-1.5 py-1.5 text-right font-mono text-[10px] tabular-nums text-ink-2">
                                {r.runtime_s}s
                              </td>
                              <td className="px-2.5 py-1.5">
                                <StatusDot status={r.status} />
                              </td>
                            </tr>
                          ))}
                          {Object.keys(data.latestByAgent).length === 0 && (
                            <tr>
                              <td colSpan={6} className="px-3 py-6 text-center text-[11px] text-ink-3">
                                No runs recorded yet for this client.
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              )}
              {!loading && !data && (
                <div className="py-10 text-center text-[11px] text-ink-3">
                  Could not load metrics for this client.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </>
  )
}
