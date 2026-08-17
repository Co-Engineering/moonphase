import { useState } from 'react'
import * as api from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * An undo button for people who do not use git.
 *
 * The thing that frightens someone who cannot read a diff is not that the
 * agent will fail — it is that it will succeed at the wrong thing and leave
 * them with no way back. Until now there wasn't one: the agent commits when it
 * feels like it, and everything else sits in a worktree they have no handle
 * on.
 *
 * So the panel says three things and nothing else: where you have saved,
 * whether there is work you have not saved, and a button to go back. The words
 * commit, branch, stash and reset never appear, and neither does a hash.
 *
 * Going back is itself saved first, which is what lets the confirmation say
 * "you can come back from this" and mean it.
 */

interface Props {
  projectId: string
  session: string
}

export function SavePoints({ projectId, session }: Props) {
  const board = useResource(() => api.checkpoints(projectId, session), [projectId, session], {
    pollMs: 20000,
  })
  const [label, setLabel] = useState('')
  const [naming, setNaming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<api.Checkpoint | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const data = board.data

  async function run(fn: () => Promise<unknown>, said: string) {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await fn()
      setNote(said)
      setLabel('')
      setNaming(false)
      setConfirming(null)
      board.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (data?.detail) {
    return (
      <div className="save-points">
        <p className="hint">{data.detail}</p>
      </div>
    )
  }

  return (
    <div className="save-points">
      <div className="save-head">
        <div>
          <h3>Save points</h3>
          <p className="hint">
            {data && data.unsaved > 0 ? (
              <>
                <strong>{data.unsaved}</strong> file{data.unsaved === 1 ? '' : 's'} changed
                since your last save point.
              </>
            ) : (
              'Somewhere to come back to if the next change goes wrong.'
            )}
          </p>
        </div>
        {naming ? (
          <form
            className="save-name"
            onSubmit={(event) => {
              event.preventDefault()
              void run(
                () => api.saveCheckpoint(projectId, session, label),
                'Saved. You can come back here whenever you like.',
              )
            }}
          >
            <input
              autoFocus
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="What works right now?"
              maxLength={120}
            />
            <button className="primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="ghost" onClick={() => setNaming(false)}>
              Cancel
            </button>
          </form>
        ) : (
          <button className="primary" disabled={busy} onClick={() => setNaming(true)}>
            Save this version
          </button>
        )}
      </div>

      {error && <div className="error">{error}</div>}
      {note && <p className="save-note">{note}</p>}

      {data && data.points.length === 0 && (
        <div className="empty">
          <h3>No save points yet</h3>
          Save one whenever the project is in a state you would hate to lose.
        </div>
      )}

      <div className="save-list">
        {(data?.points ?? []).map((point) => (
          <div key={point.id} className={`save-row${point.current ? ' current' : ''}`}>
            <span className="save-when">{when(point.at)}</span>
            <span className="save-label">
              {point.label}
              {point.automatic && (
                <span className="tag" title="Saved automatically before going back">
                  automatic
                </span>
              )}
            </span>
            {point.current ? (
              <span className="save-current">you are here</span>
            ) : (
              <button
                className="ghost small"
                disabled={busy}
                onClick={() => setConfirming(point)}
              >
                Go back to this
              </button>
            )}
          </div>
        ))}
      </div>

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(null)}>
          <div className="card modal" onClick={(event) => event.stopPropagation()}>
            <h2>Go back to “{confirming.label}”?</h2>
            <p className="hint">
              Every file goes back to how it was at that point.{' '}
              {data && data.unsaved > 0 ? (
                <>
                  Your {data.unsaved} unsaved change{data.unsaved === 1 ? '' : 's'} will be
                  saved as their own point first, so nothing is lost and you can come
                  straight back.
                </>
              ) : (
                <>
                  Where you are now stays in the list, so you can come straight back.
                </>
              )}
            </p>
            <p className="hint">
              Installed packages are left alone — you will not have to wait for anything
              to reinstall.
            </p>
            <div className="actions">
              <button className="ghost" onClick={() => setConfirming(null)}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  void run(
                    () => api.restoreCheckpoint(projectId, session, confirming.id),
                    `Back at “${confirming.label}”. Where you were is still in the list.`,
                  )
                }
              >
                {busy ? 'Going back…' : 'Go back'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/** A time someone can place, rather than one they have to decode. */
export function when(iso: string, now = new Date()): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const minutes = Math.round((now.getTime() - at.getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`
  return at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
