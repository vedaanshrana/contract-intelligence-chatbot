interface Props {
  checked: boolean
  onChange: (v: boolean) => void
  label?: string
}

/** Premium switch — primary #FF6600 when active in both modes. */
export default function Toggle({ checked, onChange, label }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-[18px] w-[32px] shrink-0 items-center rounded-full border transition-colors duration-150 ${
        checked
          ? 'border-primary bg-primary'
          : 'border-line-strong bg-surface-3'
      }`}
    >
      <span
        className={`inline-block h-[12px] w-[12px] rounded-full bg-white shadow transition-transform duration-150 ${
          checked ? 'translate-x-[16px]' : 'translate-x-[2px]'
        }`}
      />
    </button>
  )
}
