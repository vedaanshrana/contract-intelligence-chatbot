import { useMemo, useState } from 'react'
import { Minus, Plus } from 'lucide-react'

type Row = Record<string, unknown>

interface GNode {
  id: string
  label: string
  sub: string
  status: string
  x: number
  y: number
  parent?: string
}

function s(row: Row, ...keys: string[]): string {
  for (const k of keys) {
    const v = row[k]
    if (v != null && String(v).trim() && String(v).toLowerCase() !== 'nan')
      return String(v).trim()
  }
  return ''
}

function statusColor(status: string): string {
  const v = status.toLowerCase()
  if (v.includes('parent')) return 'var(--primary)'
  if (v.includes('orphan') || v.includes('misc')) return 'var(--warn)'
  if (v.includes('duplicate') || v.includes('supersed')) return 'var(--ink-3)'
  return 'var(--ok)' // child / standalone / active leaves
}

/** Colour key — mirrors statusColor() above so the legend can't drift from it. */
const LEGEND: { label: string; color: string; title: string }[] = [
  { label: 'Parent', color: 'var(--primary)', title: 'Parent / master agreement' },
  { label: 'Child', color: 'var(--ok)', title: 'Child / standalone / active leaf' },
  { label: 'Orphan', color: 'var(--warn)', title: 'Orphan / miscellaneous' },
  { label: 'Superseded', color: 'var(--ink-3)', title: 'Superseded / duplicate' },
]

/** Real contract-hierarchy graph built from the Hierarchy agent's output rows. */
export default function EntityGraph({ rows }: { rows: Row[] }) {
  const [zoom, setZoom] = useState(1)
  const [hover, setHover] = useState<string | null>(null)

  const { nodes, edges, height } = useMemo(() => {
    const W = 860
    if (!rows || rows.length === 0) return { nodes: [], edges: [], height: 240 }

    const items = rows.map((r) => ({
      id: s(r, 'Filename', 'filename'),
      type: s(r, 'Contract_Type', 'contract_type') || 'Contract',
      status: s(r, 'Hierarchy_Status', 'hierarchy_status') || 'Contract',
      parent: s(r, 'Parent_Contract', 'Parent_Filename', 'parent_contract'),
      level: Number(r['Hierarchy_Level'] ?? r['hierarchy_level'] ?? 0) || 0,
      date: s(r, 'Signed_Date', 'signed_date', 'Effective_Date'),
    })).filter((x) => x.id)

    const ids = new Set(items.map((x) => x.id))
    const maxLevel = Math.max(0, ...items.map((x) => x.level))
    const byLevel = new Map<number, typeof items>()
    for (const it of items) {
      const lvl = ids.has(it.parent) ? Math.min(it.level, maxLevel) : it.level
      if (!byLevel.has(lvl)) byLevel.set(lvl, [])
      byLevel.get(lvl)!.push(it)
    }
    const levels = [...byLevel.keys()].sort((a, b) => a - b)
    const rowH = 64
    const ns: GNode[] = []
    levels.forEach((lvl, li) => {
      const group = byLevel.get(lvl)!
      group.forEach((it, gi) => {
        ns.push({
          id: it.id,
          label: it.type,
          sub: it.date || it.status,
          status: it.status,
          x: (W / (group.length + 1)) * (gi + 1),
          y: 40 + li * rowH,
          parent: ids.has(it.parent) ? it.parent : undefined,
        })
      })
    })
    const map = new Map(ns.map((n) => [n.id, n]))
    const es = ns
      .filter((n) => n.parent && map.has(n.parent))
      .map((n) => ({ from: map.get(n.parent!)!, to: n, id: `${n.parent}->${n.id}` }))
    return { nodes: ns, edges: es, height: 40 + (levels.length || 1) * rowH + 24 }
  }, [rows])

  const connected = useMemo(() => {
    if (!hover) return null
    const set = new Set<string>([hover])
    for (const e of edges) {
      if (e.from.id === hover) set.add(e.to.id)
      if (e.to.id === hover) set.add(e.from.id)
    }
    return set
  }, [hover, edges])

  if (nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center bg-bg/40 text-center text-[11px] text-ink-3">
        Run the Hierarchy agent to see the contract relationship graph.
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col bg-bg/40">
      {/* status colour key — pinned above the scrollable graph */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line/60 px-2.5 py-1.5">
        <span className="font-mono text-[8.5px] uppercase tracking-[0.14em] text-ink-3">
          Contract Hierarchy
        </span>
        <span className="text-line-strong">·</span>
        {LEGEND.map((l) => (
          <span key={l.label} title={l.title} className="flex items-center gap-1">
            <span className="h-2 w-2 shrink-0" style={{ background: l.color }} />
            <span className="font-mono text-[8.5px] uppercase tracking-wider text-ink-3">
              {l.label}
            </span>
          </span>
        ))}
        <span className="ml-auto hidden font-mono text-[8px] uppercase tracking-wider text-ink-3/70 sm:block">
          hover to trace lineage
        </span>
      </div>

      {/* graph viewport */}
      <div className="relative min-h-0 flex-1 overflow-auto">
        <div className="absolute right-2 top-2 z-10 flex flex-col border border-line bg-surface">
          <button onClick={() => setZoom((z) => Math.min(1.8, z + 0.2))} className="flex h-6 w-6 items-center justify-center text-ink-3 transition-colors duration-150 hover:text-primary" aria-label="Zoom in">
            <Plus size={12} />
          </button>
          <button onClick={() => setZoom((z) => Math.max(0.6, z - 0.2))} className="flex h-6 w-6 items-center justify-center border-t border-line text-ink-3 transition-colors duration-150 hover:text-primary" aria-label="Zoom out">
            <Minus size={12} />
          </button>
        </div>

        <svg
          viewBox={`0 0 860 ${height}`}
          className="h-full w-full transition-transform duration-150"
          style={{ transform: `scale(${zoom})`, transformOrigin: '50% 20%' }}
        >
        {edges.map((e) => {
          const lit = connected ? connected.has(e.from.id) && connected.has(e.to.id) : false
          const dim = connected && !lit
          const midY = (e.from.y + e.to.y) / 2
          return (
            <path
              key={e.id}
              d={`M${e.from.x},${e.from.y + 14} C${e.from.x},${midY} ${e.to.x},${midY} ${e.to.x},${e.to.y - 14}`}
              fill="none"
              stroke={lit ? 'var(--primary)' : 'var(--line-strong)'}
              strokeWidth={lit ? 1.6 : 1}
              opacity={dim ? 0.25 : 1}
              className="transition-all duration-150"
            />
          )
        })}
        {nodes.map((n) => {
          const lit = connected?.has(n.id)
          const dim = connected && !lit
          const w = 116
          const h = 30
          return (
            <g
              key={n.id}
              transform={`translate(${n.x - w / 2}, ${n.y - h / 2})`}
              onMouseEnter={() => setHover(n.id)}
              onMouseLeave={() => setHover(null)}
              className="cursor-pointer transition-opacity duration-150"
              opacity={dim ? 0.3 : 1}
            >
              <title>{n.id}</title>
              <rect width={w} height={h} fill="var(--surface)" stroke={lit ? 'var(--primary)' : statusColor(n.status)} strokeWidth={lit ? 1.8 : 1.2} />
              <rect width={4} height={h} fill={statusColor(n.status)} />
              <text x={w / 2 + 2} y={h / 2 - 2} textAnchor="middle" fontSize={9} fontWeight={600} fill="var(--ink)" fontFamily="JetBrains Mono, monospace">
                {n.label.length > 16 ? n.label.slice(0, 15) + '…' : n.label}
              </text>
              <text x={w / 2 + 2} y={h / 2 + 9} textAnchor="middle" fontSize={6.5} fill="var(--ink-3)" fontFamily="JetBrains Mono, monospace">
                {n.sub.length > 20 ? n.sub.slice(0, 19) + '…' : n.sub}
              </text>
            </g>
          )
        })}
        </svg>
      </div>
    </div>
  )
}
