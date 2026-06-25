import { useEffect, useState } from 'react'
import { CheckCircle2, Flag, Loader2, X } from 'lucide-react'
import { useApp } from '../../store'
import { api } from '../../api'
import Dropdown from '../ui/Dropdown'

const CATEGORIES = [
  { value: 'Feedback', label: 'Feedback' },
  { value: 'Completely wrong', label: 'Completely wrong' },
  { value: 'Partially wrong / incomplete', label: 'Partially wrong / incomplete' },
  { value: 'Other', label: 'Other' },
]

/**
 * Modal for reporting an issue / leaving feedback on one chatbot answer.
 * Submits to POST /api/feedback, which appends a row to
 * backend/Feedbacks/feedback.xlsx along with the question, answer, and the
 * current scope context (core, clients, chat model, reporting user).
 */
export default function ReportModal({
  question,
  answer,
  onClose,
}: {
  question: string
  answer: string
  onClose: () => void
}) {
  const { core, scopeNames, settings, user } = useApp()
  const [category, setCategory] = useState('Feedback')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const submit = async () => {
    if (!title.trim() && !description.trim()) {
      setError('Add a title or a description.')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      await api.submitFeedback({
        category,
        title: title.trim(),
        description: description.trim(),
        question,
        answer,
        core,
        clients: scopeNames,
        chat_model: settings?.chat_model ?? '',
        user_name: user?.name ?? '',
        user_email: user?.email ?? '',
      })
      setDone(true)
      setTimeout(onClose, 900)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-100 flex items-center justify-center bg-black/40 backdrop-blur-md"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="animate-fade-up flex max-h-[90vh] w-[520px] max-w-[92vw] flex-col border border-line-strong bg-surface shadow-2xl shadow-black/40">
        {/* header */}
        <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <Flag size={14} className="text-primary" />
            <div>
              <div className="text-[13px] font-semibold text-ink">Report this answer</div>
              <div className="font-mono text-[9px] uppercase tracking-[0.18em] text-ink-3">
                Saved to backend · Feedbacks
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center text-ink-3 transition-colors duration-150 hover:bg-surface-2 hover:text-ink"
            aria-label="Close report dialog"
          >
            <X size={15} />
          </button>
        </div>

        {/* body */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {done ? (
            <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
              <CheckCircle2 size={30} className="text-ok" />
              <div className="text-[13px] font-medium text-ink">Thanks — feedback recorded.</div>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {/* category */}
              <div>
                <label className="mb-1 block font-mono text-[9px] uppercase tracking-wider text-ink-3">
                  Category
                </label>
                <Dropdown mono value={category} options={CATEGORIES} onChange={setCategory} />
              </div>

              {/* title */}
              <div>
                <label className="mb-1 block font-mono text-[9px] uppercase tracking-wider text-ink-3">
                  Title
                </label>
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Short summary of the issue"
                  className="w-full border border-line bg-surface-2 px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-primary"
                />
              </div>

              {/* description */}
              <div>
                <label className="mb-1 block font-mono text-[9px] uppercase tracking-wider text-ink-3">
                  Description
                </label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  placeholder="What was wrong, or what would the correct answer be?"
                  className="w-full resize-none border border-line bg-surface-2 px-2.5 py-1.5 text-[12.5px] leading-relaxed text-ink outline-none focus:border-primary"
                />
              </div>

              {/* answer context preview */}
              <div className="border border-line bg-surface-2/40">
                <div className="border-b border-line px-2.5 py-1 font-mono text-[8.5px] uppercase tracking-wider text-ink-3">
                  Answer being reported
                </div>
                <div className="max-h-24 overflow-y-auto px-2.5 py-1.5 text-[11px] leading-relaxed text-ink-3">
                  {answer.slice(0, 600) || '—'}
                  {answer.length > 600 ? '…' : ''}
                </div>
              </div>

              {error && (
                <div className="border border-bad/40 bg-bad-dim px-3 py-2 font-mono text-[10px] text-bad">
                  {error}
                </div>
              )}
            </div>
          )}
        </div>

        {/* footer */}
        {!done && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-4 py-3">
            <button
              onClick={onClose}
              className="border border-line px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-ink-2 transition-colors duration-150 hover:border-line-strong hover:text-ink"
            >
              Cancel
            </button>
            <button
              onClick={() => void submit()}
              disabled={submitting}
              className="flex items-center gap-1.5 bg-primary px-4 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-white transition-all duration-150 hover:brightness-110 disabled:opacity-40"
            >
              {submitting ? (
                <>
                  <Loader2 size={11} className="animate-spin" /> Submitting…
                </>
              ) : (
                <>
                  <Flag size={11} /> Submit report
                </>
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
