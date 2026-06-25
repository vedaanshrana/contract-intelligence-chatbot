import { useState, type FormEvent } from 'react'
import { ArrowRight, Loader2, Lock, ShieldCheck, UserRound } from 'lucide-react'
import { useApp } from '../../store'
import type { Role } from '../../types'

const PERSONAS: Record<Role, { name: string; email: string; department: string }> = {
  admin: { name: 'Vedaansh Rana', email: 'vedaansh.rana@fiserv.com', department: 'Platform Administration' },
  user: { name: 'Rudy Gupta', email: 'rudraksh.gupta@fiserv.com', department: 'Biller' },
}

function Field({
  id,
  type,
  label,
  value,
  onChange,
}: {
  id: string
  type: string
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="relative">
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder=" "
        autoComplete="off"
        className="peer w-full border border-line bg-surface-2 px-3 pb-2 pt-5 text-[13px] text-ink outline-none transition-colors duration-150 focus:border-primary"
      />
      <label
        htmlFor={id}
        className="pointer-events-none absolute left-3 top-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3 transition-all duration-150 peer-placeholder-shown:top-[15px] peer-placeholder-shown:text-[12px] peer-placeholder-shown:normal-case peer-placeholder-shown:tracking-normal peer-focus:top-1.5 peer-focus:text-[9px] peer-focus:uppercase peer-focus:tracking-[0.14em] peer-focus:text-primary"
      >
        {label}
      </label>
    </div>
  )
}

export default function LoginGateway() {
  const { login } = useApp()
  const [role, setRole] = useState<Role>('admin')
  const [email, setEmail] = useState(PERSONAS.admin.email)
  const [password, setPassword] = useState('••••••••••')
  const [busy, setBusy] = useState(false)

  const pickRole = (r: Role) => {
    setRole(r)
    setEmail(PERSONAS[r].email)
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!email || !password || busy) return
    setBusy(true)
    setTimeout(() => login({ ...PERSONAS[role], role }), 650)
  }

  return (
    <div className="relative flex h-full items-center justify-center overflow-hidden bg-bg">
      {/* gradient blur anchor */}
      <div className="pointer-events-none absolute left-1/2 top-1/3 h-[420px] w-[640px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/12 blur-[140px]" />
      <div className="pointer-events-none absolute bottom-0 right-0 h-[260px] w-[420px] rounded-full bg-primary/6 blur-[120px]" />

      <div className="animate-fade-up relative w-[400px] border border-line bg-surface shadow-2xl shadow-black/30">
        {/* header */}
        <div className="border-b border-line px-7 pb-5 pt-7">
          <div className="text-[15px] font-semibold tracking-tight text-ink">
            CONTRACT INTELLIGENCE
          </div>
          <div className="font-mono text-[9px] uppercase tracking-[0.22em] text-ink-3">
            AI enabled LLM
          </div>
        </div>

        <form onSubmit={submit} className="space-y-4 px-7 py-6">
          {/* role quick-toggle */}
          <div className="grid grid-cols-2 border border-line p-[3px]">
            {(
              [
                ['admin', 'Admin Login', ShieldCheck],
                ['user', 'Normal User', UserRound],
              ] as const
            ).map(([r, label, Icon]) => (
              <button
                key={r}
                type="button"
                onClick={() => pickRole(r)}
                className={`flex items-center justify-center gap-1.5 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider transition-colors duration-150 ${
                  role === r
                    ? 'bg-primary-dim text-primary'
                    : 'text-ink-3 hover:text-ink-2'
                }`}
              >
                <Icon size={12} />
                {label}
              </button>
            ))}
          </div>

          <Field id="email" type="text" label="Username / Email" value={email} onChange={setEmail} />
          <Field id="password" type="password" label="Password" value={password} onChange={setPassword} />

          <button
            type="submit"
            disabled={busy || !email || !password}
            className="group flex w-full items-center justify-center gap-2 bg-primary px-4 py-2.5 text-[12px] font-semibold uppercase tracking-wider text-white transition-all duration-150 hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <>
                <Lock size={12} />
                Authenticate
                <ArrowRight size={13} className="transition-transform duration-150 group-hover:translate-x-0.5" />
              </>
            )}
          </button>
        </form>

        <div className="flex items-center justify-between border-t border-line px-7 py-3">
          <span className="font-mono text-[9px] uppercase tracking-wider text-ink-3">
            SSO · SAML 2.0 · SCIM
          </span>
          <span className="font-mono text-[9px] text-ink-3">v4.2.1</span>
        </div>
      </div>
    </div>
  )
}
