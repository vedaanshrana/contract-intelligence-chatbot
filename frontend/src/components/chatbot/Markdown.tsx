import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Assistant-message markdown, styled to match the terminal/console aesthetic. */
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="space-y-2 text-[12.5px] leading-relaxed text-ink-2">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
          strong: ({ children }) => (
            <strong className="font-semibold text-ink">{children}</strong>
          ),
          em: ({ children }) => <em className="text-ink">{children}</em>,
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline decoration-primary/40 underline-offset-2 hover:decoration-primary"
            >
              {children}
            </a>
          ),
          ul: ({ children }) => (
            <ul className="list-disc space-y-1 pl-5 marker:text-ink-3">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal space-y-1 pl-5 marker:text-ink-3">{children}</ol>
          ),
          li: ({ children }) => <li className="pl-0.5">{children}</li>,
          h1: ({ children }) => (
            <h1 className="pt-1 text-[15px] font-semibold text-ink">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="pt-1 text-[14px] font-semibold text-ink">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="pt-1 text-[13px] font-semibold text-ink">{children}</h3>
          ),
          code: ({ children }) => (
            <code className="border border-line bg-surface-2 px-1 py-[1px] font-mono text-[11px] text-primary">
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="overflow-x-auto border border-line bg-surface-2 p-2.5 font-mono text-[11px] text-ink-2">
              {children}
            </pre>
          ),
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-primary/40 pl-3 text-ink-3">
              {children}
            </blockquote>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto border border-line">
              <table className="w-full border-collapse text-[11.5px]">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-surface-2">{children}</thead>,
          th: ({ children }) => (
            <th className="border border-line px-2 py-1 text-left font-mono text-[9px] uppercase tracking-wider text-ink-3">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-line px-2 py-1 text-ink-2">{children}</td>
          ),
          hr: () => <hr className="border-line" />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
