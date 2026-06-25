import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BookText,
  CheckCircle2,
  CircuitBoard,
  FileSpreadsheet,
  Loader2,
  Server,
  UploadCloud,
  X,
} from 'lucide-react'
import { useApp } from '../../store'
import { api } from '../../api'
import type { Settings } from '../../types'
import Dropdown from '../ui/Dropdown'

const MODEL_ROWS: { key: keyof Settings; name: string; desc: string }[] = [
  { key: 'chat_model', name: 'Chat Engine', desc: 'Conversational answers + source citations' },
  { key: 'hier_model', name: 'Hierarchy Agent', desc: 'Per-contract metadata + amendment tree' },
  { key: 'engagement_model', name: 'Engagement + Product Module', desc: 'Scope, signatories, product hierarchy (vision)' },
  { key: 'extr_model', name: 'Fee Description Agent', desc: 'Dollar / textual line-item extraction (vision)' },
  { key: 'match_model', name: 'Material Code Matching', desc: 'Dictionary matcher' },
  { key: 'cpi_model', name: 'CPI Terms Agent', desc: 'Annual escalation language' },
  { key: 'scope_model', name: 'Scope Triage', desc: 'Cheap per-agent contract triage' },
]

function RouterTab() {
  const { settings, config, saveSettings } = useApp()
  const [draft, setDraft] = useState<Settings | null>(settings)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => setDraft(settings), [settings])

  const modelOptions = useMemo(() => {
    const set = new Set<string>(config?.models ?? [])
    if (draft) MODEL_ROWS.forEach((r) => set.add(String(draft[r.key])))
    return [...set].filter(Boolean).map((m) => ({ value: m, label: m }))
  }, [config, draft])

  if (!draft)
    return (
      <div className="flex flex-1 items-center justify-center font-mono text-[11px] text-ink-3">
        Loading settings…
      </div>
    )

  const set = (key: keyof Settings, value: string | number) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d))
    setSaved(false)
  }

  const apply = async () => {
    setSaving(true)
    try {
      await saveSettings(draft)
      setSaved(true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="flex items-center gap-2 border-b border-line bg-surface-2/40 px-4 py-2 font-mono text-[9px] uppercase tracking-wider text-ink-3">
          <Server size={11} className="text-primary" />
          Backend: {config?.backend ?? '—'} · models apply to all clients
        </div>
        <table className="w-full">
          <thead className="sticky top-0 z-10 bg-surface-2">
            <tr className="border-b border-line text-left font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
              <th className="px-4 py-2 font-medium">Agent</th>
              <th className="w-[230px] px-4 py-2 font-medium">Target Model</th>
            </tr>
          </thead>
          <tbody>
            {MODEL_ROWS.map((r) => (
              <tr key={r.key} className="border-b border-line/60 transition-colors duration-150 hover:bg-surface-2/50">
                <td className="px-4 py-2.5">
                  <div className="text-[12px] font-medium text-ink">{r.name}</div>
                  <div className="text-[10.5px] text-ink-3">{r.desc}</div>
                </td>
                <td className="px-4 py-2.5">
                  <Dropdown
                    mono
                    value={String(draft[r.key])}
                    options={modelOptions}
                    onChange={(v) => set(r.key, v)}
                  />
                </td>
              </tr>
            ))}
            <tr className="border-b border-line/60">
              <td className="px-4 py-2.5">
                <div className="text-[12px] font-medium text-ink">Fee Description — Year Cutoff</div>
                <div className="text-[10.5px] text-ink-3">PDFs dated before this year are skipped at extraction</div>
              </td>
              <td className="px-4 py-2.5">
                <input
                  type="number"
                  min={2000}
                  max={2030}
                  value={draft.min_year}
                  onChange={(e) => set('min_year', Number(e.target.value))}
                  className="w-[110px] border border-line bg-surface-2 px-2 py-1.5 font-mono text-[12px] text-ink outline-none focus:border-primary"
                />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div className="flex shrink-0 items-center justify-between border-t border-line px-4 py-3">
        <span className="font-mono text-[10px] text-ink-3">
          {new Set(MODEL_ROWS.map((r) => String(draft[r.key]))).size} distinct models routed
        </span>
        <button
          onClick={apply}
          disabled={saving}
          className={`flex items-center gap-1.5 px-4 py-1.5 font-mono text-[10px] uppercase tracking-wider transition-colors duration-150 ${
            saved ? 'bg-ok-dim text-ok' : 'bg-primary text-white hover:brightness-110'
          }`}
        >
          {saving ? (
            <>
              <Loader2 size={11} className="animate-spin" /> Saving…
            </>
          ) : saved ? (
            <>
              <CheckCircle2 size={11} /> Configuration Applied
            </>
          ) : (
            'Apply Configuration'
          )}
        </button>
      </div>
    </div>
  )
}

function IngestionTab() {
  const { core, saveSettings } = useApp()
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.dictionary>> | null>(null)
  const [busy, setBusy] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(() => {
    if (!core) return
    api.dictionary(core).then(setInfo).catch(() => setInfo(null))
  }, [core])

  useEffect(() => refresh(), [refresh])

  const upload = useCallback(
    async (file: File) => {
      if (!core) return
      setBusy(true)
      setError('')
      try {
        const res = await api.uploadDictionary(core, file)
        await saveSettings({ dict_path: res.saved })
        refresh()
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setBusy(false)
      }
    },
    [core, refresh, saveSettings],
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
      {/* current dictionary */}
      <div className="border border-line">
        <div className="flex items-center justify-between border-b border-line bg-surface-2 px-3 py-1.5">
          <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
            Active material-code dictionary · {core}
          </span>
          {info && (
            <span
              className={`font-mono text-[9px] uppercase tracking-wider ${
                info.matchingEnabled ? 'text-ok' : 'text-warn'
              }`}
            >
              {info.matchingEnabled ? '● Matching enabled' : '○ No dictionary'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2.5 px-3 py-2.5">
          <FileSpreadsheet size={16} className={info?.resolvedName ? 'text-ok' : 'text-ink-3'} />
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-[11px] text-ink">
              {info?.resolvedName || 'No dictionary resolved for this core'}
            </div>
            <div className="truncate font-mono text-[9px] text-ink-3">
              {info?.override ? `override · ${info.override}` : info?.resolved || '—'}
            </div>
          </div>
        </div>
      </div>

      {/* upload */}
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          const f = e.dataTransfer.files?.[0]
          if (f) void upload(f)
        }}
        onClick={() => inputRef.current?.click()}
        className={`flex cursor-pointer flex-col items-center justify-center gap-2 border border-dashed px-6 py-9 text-center transition-all duration-150 ${
          dragOver ? 'border-primary bg-primary-dim' : 'border-line-strong bg-surface-2/50 hover:border-primary/60 hover:bg-surface-2'
        }`}
      >
        {busy ? (
          <Loader2 size={26} className="animate-spin text-primary" />
        ) : (
          <UploadCloud size={26} className={dragOver ? 'text-primary' : 'text-ink-3'} />
        )}
        <div className="text-[12.5px] font-medium text-ink">
          Drop a <span className="font-mono text-primary">.xlsx</span> dictionary here
        </div>
        <div className="font-mono text-[10px] uppercase tracking-wider text-ink-3">
          Saved into Input/{core}/ · sheet auto-detected
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx,.xls"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) void upload(f)
            e.target.value = ''
          }}
        />
      </div>

      {error && (
        <div className="border border-bad/40 bg-bad-dim px-3 py-2 font-mono text-[10px] text-bad">
          {error}
        </div>
      )}
      <p className="text-[11px] leading-relaxed text-ink-3">
        The Material Code Matching agent uses this dictionary to map each fee line
        item to a SAP material code. If no dictionary is set, matching is skipped
        and the chat will say so.
      </p>
    </div>
  )
}

export default function SettingsModal() {
  const { settingsOpen, setSettingsOpen } = useApp()
  const [tab, setTab] = useState<'router' | 'ingest'>('router')

  useEffect(() => {
    if (!settingsOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSettingsOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [settingsOpen, setSettingsOpen])

  if (!settingsOpen) return null

  return (
    <div
      className="fixed inset-0 z-100 flex items-center justify-center bg-black/40 backdrop-blur-md"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) setSettingsOpen(false)
      }}
    >
      <div className="animate-fade-up flex h-[560px] w-[680px] max-w-[92vw] flex-col border border-line-strong bg-surface shadow-2xl shadow-black/40">
        <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-3">
          <div>
            <div className="text-[13px] font-semibold text-ink">Platform Settings</div>
            <div className="font-mono text-[9px] uppercase tracking-[0.18em] text-ink-3">
              Workspace · Production
            </div>
          </div>
          <button
            onClick={() => setSettingsOpen(false)}
            className="flex h-7 w-7 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
            aria-label="Close settings"
          >
            <X size={15} />
          </button>
        </div>

        <div className="flex shrink-0 border-b border-line">
          {(
            [
              ['router', 'Agent LLM Router', CircuitBoard],
              ['ingest', 'Dictionary', BookText],
            ] as const
          ).map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`flex items-center gap-2 border-b-2 px-4 py-2.5 text-[12px] font-medium transition-colors duration-150 ${
                tab === id ? 'border-primary text-ink' : 'border-transparent text-ink-3 hover:text-ink-2'
              }`}
            >
              <Icon size={13} className={tab === id ? 'text-primary' : ''} />
              {label}
            </button>
          ))}
        </div>

        {tab === 'router' ? <RouterTab /> : <IngestionTab />}
      </div>
    </div>
  )
}
