import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'

export interface DropdownOption {
  value: string
  label: string
  hint?: string
}

interface Props {
  value: string
  options: DropdownOption[]
  onChange: (v: string) => void
  searchable?: boolean
  buttonClassName?: string
  menuClassName?: string
  mono?: boolean
  renderButton?: (selected: DropdownOption | undefined) => React.ReactNode
}

/** Headless premium dropdown — sharp borders, 150ms transitions, keyboard-safe. */
export default function Dropdown({
  value,
  options,
  onChange,
  searchable,
  buttonClassName = '',
  menuClassName = '',
  mono,
  renderButton,
}: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => {
    if (open && searchable) inputRef.current?.focus()
    if (!open) setQuery('')
  }, [open, searchable])

  const filtered = useMemo(
    () =>
      query
        ? options.filter((o) =>
            o.label.toLowerCase().includes(query.toLowerCase()),
          )
        : options,
    [options, query],
  )

  const selected = options.find((o) => o.value === value)

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`group flex w-full items-center justify-between gap-2 border border-line bg-surface px-2.5 py-1.5 text-left text-[12px] font-medium text-ink transition-colors duration-150 hover:border-line-strong ${open ? 'border-primary!' : ''} ${buttonClassName}`}
      >
        {renderButton ? (
          renderButton(selected)
        ) : (
          <span className={`truncate ${mono ? 'font-mono text-[11px]' : ''}`}>
            {selected?.label ?? '—'}
          </span>
        )}
        <ChevronDown
          size={13}
          className={`shrink-0 text-ink-3 transition-transform duration-150 ${open ? 'rotate-180 text-primary' : ''}`}
        />
      </button>

      {open && (
        <div
          className={`animate-fade-up absolute left-0 right-0 z-50 mt-1 border border-line-strong bg-surface shadow-xl shadow-black/20 ${menuClassName}`}
        >
          {searchable && (
            <div className="flex items-center gap-1.5 border-b border-line px-2.5 py-1.5">
              <Search size={12} className="text-ink-3" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter…"
                className="w-full bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-3"
              />
            </div>
          )}
          <div className="max-h-56 overflow-y-auto py-1">
            {filtered.length === 0 && (
              <div className="px-3 py-2 text-[11px] text-ink-3">No matches</div>
            )}
            {filtered.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => {
                  onChange(o.value)
                  setOpen(false)
                }}
                className={`flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-[12px] transition-colors duration-150 hover:bg-surface-2 ${
                  o.value === value ? 'text-primary' : 'text-ink'
                }`}
              >
                <span className="min-w-0">
                  <span className={`block truncate ${mono ? 'font-mono text-[11px]' : ''}`}>
                    {o.label}
                  </span>
                  {o.hint && (
                    <span className="block truncate text-[10px] text-ink-3">
                      {o.hint}
                    </span>
                  )}
                </span>
                {o.value === value && <Check size={12} className="shrink-0" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
