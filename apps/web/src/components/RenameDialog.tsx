import { useState, type FormEvent } from 'react'

/**
 * Rename one thing.
 *
 * The name only, and the note says so: a project's slug, container and volumes
 * were derived from its name when it was created and are what the running
 * container is actually called. Renaming those would mean recreating it, which
 * is a great deal to do to somebody fixing a typo.
 */
export function RenameDialog({
  what,
  current,
  note,
  onRename,
  onClose,
}: {
  what: string
  current: string
  note?: string
  onRename: (name: string) => Promise<unknown>
  onClose: () => void
}) {
  const [name, setName] = useState(current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const next = name.trim()
    // Unchanged or emptied: nothing to do, and doing nothing is not a failure.
    if (!next || next === current.trim()) {
      onClose()
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onRename(next)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>Rename {what}</h2>

        <form onSubmit={submit}>
          {error && <div className="banner error">{error}</div>}

          <label>
            <span>Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={64}
              autoFocus
            />
          </label>
          {note && (
            <p className="hint" style={{ marginTop: -6 }}>
              {note}
            </p>
          )}

          <div className="actions">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <div className="spacer" />
            <button type="button" onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
