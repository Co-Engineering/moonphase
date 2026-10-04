import { useEffect, useState } from 'react'
import { api, type Leftovers } from '../lib/api'
import { formatBytes } from '../lib/bytes'

// Folders of sessions that no longer exist. A scan `du`s the project's whole
// session area, so it runs only when asked and the result is shown before
// anything is deleted.
export function LeftoverCleanup({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [found, setFound] = useState<Leftovers | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  const [freed, setFreed] = useState<number | null>(null)

  useEffect(() => {
    let live = true
    api
      .leftovers(projectId)
      .then((result) => live && setFound(result))
      .catch((err) => live && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => live && setBusy(false))
    return () => {
      live = false
    }
  }, [projectId])

  const clean = async () => {
    if (!found) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.cleanLeftovers(
        projectId,
        found.sessions.map((s) => s.name),
      )
      setFreed(result.freed_bytes)
      setFound(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="leftover-cleanup">
      {error && <div className="banner error">{error}</div>}
      {freed !== null ? (
        <p className="hint">
          Freed {formatBytes(freed)}. The bars above catch up at the next sample, within a
          few minutes.{' '}
          <button className="link" onClick={onClose}>
            Done
          </button>
        </p>
      ) : busy && !found ? (
        <p className="hint">Looking for folders left behind by deleted sessions…</p>
      ) : found && found.sessions.length === 0 ? (
        <p className="hint">
          Nothing left over — every session folder belongs to a session that still
          exists.{' '}
          <button className="link" onClick={onClose}>
            Close
          </button>
        </p>
      ) : found ? (
        <>
          <p className="hint">
            {found.sessions.length} folder{found.sessions.length === 1 ? '' : 's'} from
            deleted sessions, {formatBytes(found.total_bytes)} in total. Their git branches
            are kept; caches, uploads and checkouts are deleted.
          </p>
          <ul className="leftover-list">
            {found.sessions.map((s) => (
              <li key={s.name}>
                <span>{s.name}</span>
                <span className="hint">
                  {formatBytes(s.bytes)} · last changed{' '}
                  {new Date(s.modified_at).toLocaleDateString()}
                </span>
              </li>
            ))}
          </ul>
          <span className="row-menu-confirm">
            <button className="danger" disabled={busy} onClick={() => void clean()}>
              Delete {found.sessions.length} folder{found.sessions.length === 1 ? '' : 's'}
            </button>
            <button disabled={busy} onClick={onClose}>
              Cancel
            </button>
          </span>
        </>
      ) : null}
    </div>
  )
}
