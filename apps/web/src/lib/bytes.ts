const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

/** Decimal (1000-based), matching what `df` and Docker's own CLI report. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const exponent = Math.min(Math.floor(Math.log10(bytes) / 3), UNITS.length - 1)
  const value = bytes / 1000 ** exponent
  const precision = exponent === 0 ? 0 : value < 10 ? 2 : 1
  return `${value.toFixed(precision)} ${UNITS[exponent]}`
}
