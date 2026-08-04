// Shared WIB (UTC+7, Asia/Jakarta) helpers — backend timestamps are naive
// UTC ISO strings (no "Z"/offset suffix), so they must be explicitly parsed
// as UTC before any timezone conversion, otherwise the browser's Date
// constructor treats them as local time.

const HAS_TZ_SUFFIX = /Z|[+-]\d{2}:\d{2}$/

function parseUtc(iso) {
  return new Date(HAS_TZ_SUFFIX.test(iso) ? iso : `${iso}Z`)
}

// WIB calendar date (YYYY-MM-DD) for "now" or a given Date.
export function wibDateStr(d = new Date()) {
  const wib = new Date(d.getTime() + 7 * 60 * 60 * 1000)
  return wib.toISOString().slice(0, 10)
}

// Format a backend (naive UTC) ISO timestamp as a WIB date+time string.
export function formatWib(iso) {
  try {
    return parseUtc(iso).toLocaleString('id-ID', {
      timeZone: 'Asia/Jakarta',
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

// Format a backend (naive UTC) ISO timestamp (or a Date) as WIB "HH:mm" —
// used for the live rolling-window chart's axis labels.
export function formatWibTime(isoOrDate) {
  try {
    const d = isoOrDate instanceof Date ? isoOrDate : parseUtc(isoOrDate)
    return d.toLocaleTimeString('id-ID', {
      timeZone: 'Asia/Jakarta',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}
