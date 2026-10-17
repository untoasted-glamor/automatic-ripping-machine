// `H:MM:SS`, or `M:SS` under an hour (1452 → "24:12", 13078 → "3:37:58").
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  if (h === 0) return `${m}:${ss}`
  return `${h}:${String(m).padStart(2, '0')}:${ss}`
}

// Mirrors JobDetail's `trackSize`: the post-rip actual wins; otherwise
// MakeMKV's scan-time estimate prefixed with `~`; `—` when neither is known.
export function trackDuration(actual: number | null, expected: number | null): string {
  if (actual !== null) return formatDuration(actual)
  if (expected !== null) return `~${formatDuration(expected)}`
  return '—'
}
