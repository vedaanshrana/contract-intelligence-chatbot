import type { ReactNode } from 'react'

/** Lightweight CSS tooltip shown to the right — used by the collapsed sidebar rail. */
export default function Tooltip({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <span className="group/tt relative inline-flex">
      {children}
      <span className="pointer-events-none absolute left-full top-1/2 z-50 ml-2 -translate-y-1/2 whitespace-nowrap border border-line-strong bg-surface px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-ink opacity-0 shadow-lg transition-opacity duration-150 group-hover/tt:opacity-100">
        {label}
      </span>
    </span>
  )
}
