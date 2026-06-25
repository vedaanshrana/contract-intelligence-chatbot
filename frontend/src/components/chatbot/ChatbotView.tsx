import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { ArrowUp, FileText, Flag, PanelRightOpen, Plus, Receipt, Snowflake } from 'lucide-react'
import { useApp } from '../../store'
import { api } from '../../api'
import { SUGGESTIONS } from '../../constants'
import type { ChatCitation, ChatMessage, ChatSession } from '../../types'
import Toggle from '../ui/Toggle'
import Markdown from './Markdown'
import PdfViewer, { type PdfDoc, type PdfFocus } from './PdfViewer'
import ReportModal from './ReportModal'

let msgSeq = 0
let focusSeq = 0
const newId = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `id-${Date.now()}-${++msgSeq}`

function CitationChips({
  citations,
  onOpen,
}: {
  citations: ChatCitation[]
  onOpen: (c: ChatCitation) => void
}) {
  if (citations.length === 0) return null
  return (
    <div className="mt-2 border-t border-line pt-2">
      <div className="pb-1 font-mono text-[8.5px] uppercase tracking-[0.16em] text-ink-3">
        Open cited contracts
      </div>
      <div className="flex flex-wrap gap-1.5">
        {citations.map((c) => (
          <button
            key={`${c.client}/${c.name}`}
            onClick={() => onOpen(c)}
            className="flex items-center gap-1 border border-primary/35 bg-primary-dim px-1.5 py-[2px] font-mono text-[10px] text-primary transition-all duration-150 hover:border-primary hover:brightness-110"
            title={`${c.client} — open ${c.name}${c.page ? ` at p.${c.page}` : ''}`}
          >
            <FileText size={9} />
            <span className="max-w-[220px] truncate">{c.name}</span>
            {c.page != null && <span className="opacity-70">p.{c.page}</span>}
          </button>
        ))}
      </div>
    </div>
  )
}

function InvoiceChips({ invoices }: { invoices: { doc: string; url: string }[] }) {
  if (invoices.length === 0) return null
  return (
    <div className="mt-2 border-t border-line pt-2">
      <div className="pb-1 font-mono text-[8.5px] uppercase tracking-[0.16em] text-ink-3">
        Cited SAP invoices
      </div>
      <div className="flex flex-wrap gap-1.5">
        {invoices.map((iv) => (
          <a
            key={iv.doc + iv.url}
            href={iv.url || undefined}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 border border-line bg-surface-2 px-1.5 py-[2px] font-mono text-[10px] text-ink-2 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
          >
            <Receipt size={9} />
            {iv.doc}
          </a>
        ))}
      </div>
    </div>
  )
}

function Bubble({
  msg,
  onOpenCitation,
  onReport,
}: {
  msg: ChatMessage
  onOpenCitation: (c: ChatCitation) => void
  onReport?: () => void
}) {
  const isUser = msg.role === 'user'
  return (
    <div className={`animate-fade-up flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[88%] border px-3.5 py-2.5 ${
          isUser
            ? 'border-primary/25 bg-primary-dim text-ink'
            : msg.error
              ? 'border-bad/40 bg-bad-dim text-ink-2'
              : 'border-line bg-surface text-ink-2'
        }`}
      >
        {!isUser && (
          <div className="pb-1 font-mono text-[8.5px] uppercase tracking-[0.16em] text-ink-3">
            Contraxis · Cognitive Engine
          </div>
        )}
        {isUser ? (
          <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed">{msg.content}</p>
        ) : (
          <Markdown>{msg.content}</Markdown>
        )}
        {!isUser && msg.citations && (
          <CitationChips citations={msg.citations} onOpen={onOpenCitation} />
        )}
        {!isUser && msg.invoices && <InvoiceChips invoices={msg.invoices} />}
        {!isUser && !msg.error && onReport && (
          <div className="mt-2 flex justify-end border-t border-line pt-1.5">
            <button
              onClick={onReport}
              className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-wider text-ink-3 transition-colors duration-150 hover:text-primary"
              title="Report an issue or leave feedback on this answer"
            >
              <Flag size={9} /> Report
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1 border border-line bg-surface px-3.5 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 rounded-full bg-primary"
            style={{ animation: `dot-bounce 1.1s ${i * 0.18}s ease-in-out infinite` }}
          />
        ))}
      </div>
    </div>
  )
}

const MAX_LINES = 6
const LINE_HEIGHT = 20

/** Time-of-day greeting based on the user's system clock. */
function greetingForNow(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good Morning'
  if (h < 18) return 'Good Afternoon'
  return 'Good Evening'
}

/** Thin rail shown when the document viewer is collapsed (mirrors the sidebar). */
function CollapsedPdfRail({ count, onExpand }: { count: number; onExpand: () => void }) {
  return (
    <div className="flex h-full w-full flex-col items-center gap-2 border-l border-line bg-surface-2/40 py-3">
      <button
        onClick={onExpand}
        className="flex h-8 w-8 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-primary"
        title="Expand document viewer"
        aria-label="Expand document viewer"
      >
        <PanelRightOpen size={15} />
      </button>
      <button
        onClick={onExpand}
        className="flex h-8 w-8 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-primary"
        title="Expand document viewer"
        aria-label="Open documents"
      >
        <FileText size={15} />
      </button>
      {count > 0 && <span className="font-mono text-[9px] text-ink-3">{count}</span>}
    </div>
  )
}

export default function ChatbotView() {
  const {
    core,
    scopeNames,
    settings,
    sessions,
    activeSessionId,
    setActiveSessionId,
    upsertSession,
  } = useApp()

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [typing, setTyping] = useState(false)
  const [snowflake, setSnowflake] = useState(true)
  const [docs, setDocs] = useState<PdfDoc[]>([])
  const [focus, setFocus] = useState<PdfFocus | null>(null)
  const [pdfCollapsed, setPdfCollapsed] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [reportTarget, setReportTarget] = useState<{ question: string; answer: string } | null>(null)

  const taRef = useRef<HTMLTextAreaElement>(null)
  const threadRef = useRef<HTMLDivElement>(null)

  const focusClients = scopeNames

  // Fetch PDF docs for every client in focus (all share the current core).
  useEffect(() => {
    let alive = true
    if (focusClients.length === 0 || !core) {
      setDocs([])
      return
    }
    Promise.all(focusClients.map((c) => api.pdfs(c, core).catch(() => ({ core, pdfs: [] }))))
      .then((results) => {
        if (!alive) return
        const all: PdfDoc[] = []
        results.forEach((r, i) => {
          for (const p of r.pdfs)
            all.push({ client: focusClients[i], core: r.core || core, name: p.name, label: p.label })
        })
        setDocs(all)
      })
    return () => {
      alive = false
    }
  }, [focusClients.join('|'), core]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load a saved session when one is opened from the sidebar.
  useEffect(() => {
    if (activeSessionId && activeSessionId !== sessionId) {
      const s = sessions.find((x) => x.id === activeSessionId)
      if (s) {
        setMessages(s.messages)
        setSessionId(s.id)
        setFocus(null)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, typing])

  const autosize = () => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, MAX_LINES * LINE_HEIGHT + 16)}px`
  }

  const persist = useCallback(
    (msgs: ChatMessage[], id: string) => {
      const firstUser = msgs.find((m) => m.role === 'user')
      const title = firstUser ? firstUser.content.slice(0, 80) : 'New conversation'
      const session: ChatSession = {
        id,
        title,
        core,
        focus: focusClients,
        messages: msgs,
        updatedAt: Date.now(),
      }
      upsertSession(session)
    },
    [core, focusClients, upsertSession],
  )

  const send = async (text?: string) => {
    const content = (text ?? draft).trim()
    if (!content || typing || focusClients.length === 0) return

    const id = sessionId ?? newId()
    if (!sessionId) {
      setSessionId(id)
      setActiveSessionId(id)
    }

    const userMsg: ChatMessage = { id: newId(), role: 'user', content }
    const withUser = [...messages, userMsg]
    setMessages(withUser)
    setDraft('')
    if (taRef.current) taRef.current.style.height = 'auto'
    setTyping(true)

    try {
      const history = withUser.map((m) => ({ role: m.role, content: m.content }))
      const res = await api.chat(focusClients, history, snowflake)
      const assistantMsg: ChatMessage = {
        id: newId(),
        role: 'assistant',
        content: res.reply,
        citations: res.citations,
        invoices: res.invoices,
      }
      const finalMsgs = [...withUser, assistantMsg]
      setMessages(finalMsgs)
      persist(finalMsgs, id)
    } catch (e) {
      const errMsg: ChatMessage = {
        id: newId(),
        role: 'assistant',
        content: `⚠️ ${(e as Error).message}`,
        error: true,
      }
      const finalMsgs = [...withUser, errMsg]
      setMessages(finalMsgs)
      persist(finalMsgs, id)
    } finally {
      setTyping(false)
    }
  }

  const newChat = () => {
    setMessages([])
    setSessionId(null)
    setActiveSessionId(null)
    setFocus(null)
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  const openCitation = (c: ChatCitation) => {
    setPdfCollapsed(false) // clicking a source name always opens the viewer
    setFocus({ client: c.client, core: c.core || core, name: c.name, page: c.page, seq: ++focusSeq })
  }

  const greeting = greetingForNow()
  const focusLabel = useMemo(() => {
    if (focusClients.length === 0) return 'No clients selected'
    if (focusClients.length === 1) return focusClients[0]
    return `${focusClients.length} clients in focus`
  }, [focusClients])

  return (
    <>
    <div
      className="grid h-full min-h-0"
      style={{ gridTemplateColumns: pdfCollapsed ? '1fr 44px' : '1fr 1fr' }}
    >
      {/* ── Left pane: conversation ────────────────────────────── */}
      <div className="flex min-h-0 min-w-0 flex-col border-r border-line">
        {/* focus header */}
        <div className="flex shrink-0 items-center justify-between border-b border-line bg-surface px-4 py-2">
          <div className="flex min-w-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-ink-3">
            <span className="text-primary">●</span>
            <span className="truncate">{core}</span>
            <span className="text-line-strong">/</span>
            <span className="truncate text-ink-2">{focusLabel}</span>
          </div>
          <button
            onClick={newChat}
            className="flex items-center gap-1.5 border border-line px-2.5 py-1 font-mono text-[9.5px] uppercase tracking-wider text-ink-2 transition-colors duration-150 hover:border-primary/50 hover:text-primary"
          >
            <Plus size={11} />
            New chat
          </button>
        </div>

        <div ref={threadRef} className="min-h-0 flex-1 overflow-y-auto p-5">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <div className="font-mono text-[9px] uppercase tracking-[0.24em] text-primary">
                Cognitive Engine · Online
              </div>
              <h2 className="pt-3 text-[28px] font-semibold tracking-tight text-ink">
                {greeting}
              </h2>
              <p className="max-w-sm pt-1.5 text-[12.5px] leading-relaxed text-ink-3">
                {focusClients.length === 0
                  ? 'Select one or more clients in the sidebar to ground the conversation in their contracts.'
                  : 'Ask anything about the contracts in your selected scope. Answers cite source documents you can open inline.'}
              </p>
              {focusClients.length > 0 && (
                <div className="grid w-full max-w-md grid-cols-1 gap-1.5 pt-6">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      onClick={() => void send(s)}
                      className="border border-line bg-surface px-3 py-2 text-left text-[12px] text-ink-2 transition-colors duration-150 hover:border-primary/50 hover:text-ink"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-3">
              {messages.map((m, idx) => (
                <Bubble
                  key={m.id}
                  msg={m}
                  onOpenCitation={openCitation}
                  onReport={
                    m.role === 'assistant' && !m.error
                      ? () => {
                          // The question = the most recent user message above it.
                          let q = ''
                          for (let i = idx - 1; i >= 0; i--) {
                            if (messages[i].role === 'user') {
                              q = messages[i].content
                              break
                            }
                          }
                          setReportTarget({ question: q, answer: m.content })
                        }
                      : undefined
                  }
                />
              ))}
              {typing && <TypingIndicator />}
            </div>
          )}
        </div>

        {/* ── Input action console ─────────────────────────────── */}
        <div className="shrink-0 border-t border-line bg-surface p-3">
          {/* snowflake pipeline toggle */}
          <div className="flex items-center justify-between border border-line bg-surface-2/60 px-3 py-1.5">
            <div className="flex items-center gap-2">
              <Snowflake
                size={13}
                className={`transition-colors duration-150 ${snowflake ? 'text-primary' : 'text-ink-3'}`}
              />
              <span className="text-[11px] font-medium text-ink-2">SAP Invoice Context</span>
              <span
                className={`font-mono text-[8.5px] uppercase tracking-wider transition-colors duration-150 ${
                  snowflake ? 'text-ok' : 'text-ink-3'
                }`}
              >
                {snowflake ? '● Snowflake on (invoice questions)' : '○ Disabled'}
              </span>
            </div>
            <Toggle checked={snowflake} onChange={setSnowflake} label="Snowflake invoice context" />
          </div>

          {/* input box — the wrapper's border is the single focus indicator;
              the textarea's own focus outline is suppressed so focus shows as
              one outer orange box, not two nested boxes */}
          <div className="mt-2 border border-line bg-surface-2/40 transition-colors duration-150 focus-within:border-primary">
            <textarea
              ref={taRef}
              rows={1}
              value={draft}
              disabled={focusClients.length === 0}
              onChange={(e) => {
                setDraft(e.target.value)
                autosize()
              }}
              onKeyDown={onKey}
              placeholder={
                focusClients.length === 0
                  ? 'Select clients in the sidebar to begin…'
                  : 'Query contracts, clauses, codes, obligations…'
              }
              style={{ outline: 'none' }}
              className="block max-h-[136px] w-full resize-none overflow-y-auto bg-transparent px-3 pt-2.5 text-[12.5px] leading-[20px] text-ink outline-none placeholder:text-ink-3 disabled:opacity-50"
            />
            <div className="flex items-center gap-1 px-2 pb-2 pt-1">
              <span className="ml-1 flex items-center gap-1 font-mono text-[9px] text-ink-3">
                {settings?.chat_model ?? 'model'}
              </span>
              <div className="flex-1" />
              <span className="hidden font-mono text-[9px] text-ink-3 sm:block">
                ⏎ send · ⇧⏎ newline
              </span>
              <button
                onClick={() => void send()}
                disabled={!draft.trim() || typing || focusClients.length === 0}
                className="flex h-7 w-7 items-center justify-center bg-primary text-white transition-all duration-150 hover:brightness-110 disabled:opacity-30"
                aria-label="Send message"
              >
                <ArrowUp size={14} strokeWidth={2.5} />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── Right pane: document viewer (collapsible) ──────────── */}
      {pdfCollapsed ? (
        <CollapsedPdfRail count={docs.length} onExpand={() => setPdfCollapsed(false)} />
      ) : (
        <PdfViewer docs={docs} focus={focus} onCollapse={() => setPdfCollapsed(true)} />
      )}
    </div>
    {reportTarget && (
      <ReportModal
        question={reportTarget.question}
        answer={reportTarget.answer}
        onClose={() => setReportTarget(null)}
      />
    )}
    </>
  )
}
