import { useEffect, useState } from 'react'
import * as api from '../lib/api'

/**
 * Finding the moment something happened.
 *
 * After a week of sessions the thing you remember is not which project it was
 * in — it is a phrase. "Where did I tell it about the rate limiter." Scrolling
 * four transcripts to find that is the work the transcript was supposed to
 * save.
 *
 * Searched on demand rather than as you type: each keystroke would be a grep
 * across every container you own, over SSH. A deliberate Enter is both cheaper
 * and closer to how the question actually arrives.
 */

interface Props {
  onOpen: (projectId: string, session: string) => void
  onClose: () => void
}

export function Search({ onOpen, onClose }: Props) {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState<api.SearchResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function run() {
    if (query.trim().length < 2) return
    setBusy(true)
    setError(null)
    try {
      setResult(await api.searchTranscripts(query.trim()))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal modal--wide" onClick={(event) => event.stopPropagation()}>
        <div className="row-between">
          <h2>Search your sessions</h2>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>

        <form
          className="search-form"
          onSubmit={(event) => {
            event.preventDefault()
            void run()
          }}
        >
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="A phrase you remember…"
          />
          <button className="primary" disabled={busy || query.trim().length < 2}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </form>

        {error && <div className="error">{error}</div>}

        {result && (
          <>
            {result.partial && (
              <p className="hint">
                One machine did not answer in time, so this list may be missing hits from
                it.
              </p>
            )}
            {result.hits.length === 0 ? (
              <div className="empty">
                <h3>Nothing matched “{result.query}”</h3>
                Only your own sessions are searched, and only what was said — not what
                tools returned.
              </div>
            ) : (
              <div className="hits">
                {result.hits.map((hit, index) => (
                  <button
                    key={`${hit.session}-${hit.at}-${index}`}
                    className="hit"
                    onClick={() => onOpen(hit.project_id, hit.session)}
                  >
                    <span className="hit-head">
                      <span className={`hit-role role-${hit.role}`}>{hit.role}</span>
                      <span className="hit-where">
                        {hit.project_name}
                        <span className="attention-session">{hit.session}</span>
                      </span>
                      <span className="muted">{when(hit.at)}</span>
                    </span>
                    <Highlighted text={hit.text} needle={result.query} />
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function when(iso: string): string {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  return at.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/**
 * Mark the match inside the snippet.
 *
 * Without it the reader has to find their own phrase in a wall of text, which
 * is the job they came here to have done.
 */
export function segments(text: string, needle: string): { text: string; hit: boolean }[] {
  if (!needle) return [{ text, hit: false }]
  const out: { text: string; hit: boolean }[] = []
  const lower = text.toLowerCase()
  const target = needle.toLowerCase()
  let at = 0
  for (;;) {
    const found = lower.indexOf(target, at)
    if (found < 0) break
    if (found > at) out.push({ text: text.slice(at, found), hit: false })
    out.push({ text: text.slice(found, found + needle.length), hit: true })
    at = found + needle.length
  }
  if (at < text.length) out.push({ text: text.slice(at), hit: false })
  return out
}

function Highlighted({ text, needle }: { text: string; needle: string }) {
  return (
    <span className="hit-text">
      {segments(text, needle).map((part, index) =>
        part.hit ? (
          <mark key={index}>{part.text}</mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </span>
  )
}
