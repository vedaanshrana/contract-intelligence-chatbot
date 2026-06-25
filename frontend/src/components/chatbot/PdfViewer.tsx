import { useEffect, useMemo, useState } from 'react'
import { Download, ExternalLink, FileText, PanelRightClose } from 'lucide-react'
import { api } from '../../api'
import Dropdown from '../ui/Dropdown'

export interface PdfDoc {
  client: string
  core: string
  name: string
  label: string
}

export interface PdfFocus {
  client: string
  core: string
  name: string
  page: number | null
  /** monotonically increasing so re-clicking the same citation re-navigates */
  seq: number
}

function keyOf(d: { client: string; name: string }) {
  return `${d.client}::${d.name}`
}

export default function PdfViewer({
  docs,
  focus,
  onCollapse,
}: {
  docs: PdfDoc[]
  focus: PdfFocus | null
  onCollapse?: () => void
}) {
  const [selectedKey, setSelectedKey] = useState<string>('')
  const [page, setPage] = useState<number | null>(null)
  const [reloadSeq, setReloadSeq] = useState(0)

  // Default selection to the first doc when the doc list arrives/changes.
  useEffect(() => {
    if (docs.length === 0) {
      setSelectedKey('')
      return
    }
    setSelectedKey((prev) =>
      prev && docs.some((d) => keyOf(d) === prev) ? prev : keyOf(docs[0]),
    )
  }, [docs])

  // A citation focus → switch to that doc + jump to its page.
  useEffect(() => {
    if (!focus) return
    const match = docs.find((d) => d.client === focus.client && d.name === focus.name)
    if (match) {
      setSelectedKey(keyOf(match))
      setPage(focus.page ?? null)
      setReloadSeq((s) => s + 1)
    }
  }, [focus, docs])

  const selected = useMemo(
    () => docs.find((d) => keyOf(d) === selectedKey) ?? null,
    [docs, selectedKey],
  )

  const src = selected
    ? api.pdfUrl(selected.client, selected.name, selected.core, page)
    : ''

  return (
    <div className="relative flex h-full min-h-0 min-w-0 flex-col bg-surface-2/40">
      {/* viewer header */}
      <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface px-3 py-2">
        {onCollapse && (
          <button
            onClick={onCollapse}
            className="flex h-6 w-6 shrink-0 items-center justify-center text-ink-3 transition-colors duration-150 hover:text-primary"
            title="Collapse document viewer"
            aria-label="Collapse document viewer"
          >
            <PanelRightClose size={14} />
          </button>
        )}
        <FileText size={13} className="shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <Dropdown
            value={selectedKey}
            onChange={(v) => {
              setSelectedKey(v)
              setPage(null)
              setReloadSeq((s) => s + 1)
            }}
            options={docs.map((d) => ({
              value: keyOf(d),
              label: d.name,
              hint: d.client,
            }))}
          />
        </div>
        {selected && (
          <>
            {page != null && (
              <span className="hidden font-mono text-[10px] uppercase tracking-wider text-primary sm:block">
                p.{page}
              </span>
            )}
            <a
              href={src}
              target="_blank"
              rel="noreferrer"
              className="flex h-6 w-6 items-center justify-center text-ink-3 transition-colors duration-150 hover:text-primary"
              title="Open in new tab"
            >
              <ExternalLink size={13} />
            </a>
            <a
              href={api.pdfUrl(selected.client, selected.name, selected.core)}
              download={selected.name}
              className="flex h-6 w-6 items-center justify-center text-ink-3 transition-colors duration-150 hover:text-primary"
              title="Download PDF"
            >
              <Download size={13} />
            </a>
          </>
        )}
      </div>

      {/* document surface */}
      <div className="min-h-0 flex-1">
        {selected ? (
          <iframe
            key={`${selectedKey}-${reloadSeq}`}
            src={src}
            title={selected.name}
            className="h-full w-full border-0 bg-white"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
            <FileText size={26} className="text-ink-3" />
            <div className="text-[12px] text-ink-3">
              {docs.length === 0
                ? 'No contract PDFs for the selected clients'
                : 'Select a contract to view'}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
