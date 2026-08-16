import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type Share, type ShareRole } from '../lib/api'

interface Props {
  kind: 'servers' | 'projects'
  id: string
  name: string
  onClose: () => void
  /** Access may have changed for the current user (they left something). */
  onChanged: () => void
}

const ROLE_LABEL: Record<ShareRole, string> = {
  viewer: 'Can view',
  collaborator: 'Can use',
}

const WHAT_IT_MEANS: Record<'servers' | 'projects', Record<ShareRole, string>> = {
  servers: {
    viewer: 'See the machine and how it is doing.',
    collaborator: 'Also create their own projects on it.',
  },
  projects: {
    viewer: 'Watch the feed and the terminal, read-only.',
    collaborator: 'Also type into it, answer prompts, start and stop it.',
  },
}

/**
 * Giving one person access to one thing.
 *
 * Addressed by email rather than by picking from a list of users, because on a
 * self-hosted instance the person you want may not have signed up yet. The
 * grant is stored against the address either way and activates when they do,
 * so "share it and tell them to register" works in that order.
 */
export function Share({ kind, id, name, onClose, onChanged }: Props) {
  const [shares, setShares] = useState<Share[] | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<ShareRole>('collaborator')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setShares(await api.shares(kind, id))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setShares([])
    }
  }, [kind, id])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (fn: () => Promise<unknown>, { reload = true } = {}) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      if (reload) await load()
      onChanged()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err),
      )
    } finally {
      setBusy(false)
    }
  }

  const noun = kind === 'servers' ? 'server' : 'project'

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal share-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Share {name}</h2>
        <p className="hint">
          {kind === 'servers'
            ? 'People you share a server with can run their own work on it. Their projects stay theirs — you will see that they exist, not what they are doing.'
            : 'People you share a project with join the same session you are in. Anything they type lands in the same place as anything you type.'}
        </p>

        <form
          className="share-add"
          onSubmit={(e) => {
            e.preventDefault()
            if (!email.trim()) return
            void act(async () => {
              await api.addShare(kind, id, email.trim(), role)
              setEmail('')
            })
          }}
        >
          <input
            type="email"
            value={email}
            placeholder="colleague@example.com"
            onChange={(e) => setEmail(e.target.value)}
            disabled={busy}
            autoFocus
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as ShareRole)}
            disabled={busy}
          >
            <option value="collaborator">{ROLE_LABEL.collaborator}</option>
            <option value="viewer">{ROLE_LABEL.viewer}</option>
          </select>
          <button className="primary" type="submit" disabled={busy || !email.trim()}>
            Share
          </button>
        </form>
        <p className="hint share-meaning">{WHAT_IT_MEANS[kind][role]}</p>

        {error && <div className="banner error">{error}</div>}

        <div className="share-list">
          {shares === null ? (
            <p className="hint">Loading…</p>
          ) : shares.length === 0 ? (
            <p className="hint">
              Not shared with anyone. Only you and your organization can reach this{' '}
              {noun}.
            </p>
          ) : (
            shares.map((share) => (
              <div className="share-row" key={share.id}>
                <div className="share-who">
                  <span className="share-email">{share.email}</span>
                  {!share.accepted && (
                    <span
                      className="share-pending"
                      title="No account with this address yet. Access starts the moment they sign up."
                    >
                      invited
                    </span>
                  )}
                  {share.is_you && <span className="share-pending">you</span>}
                </div>
                <select
                  value={share.role}
                  disabled={busy || share.is_you}
                  onChange={(e) =>
                    void act(() =>
                      api.setShareRole(kind, id, share.id, e.target.value as ShareRole),
                    )
                  }
                >
                  <option value="collaborator">{ROLE_LABEL.collaborator}</option>
                  <option value="viewer">{ROLE_LABEL.viewer}</option>
                </select>
                <button
                  className="ghost danger"
                  disabled={busy}
                  title={share.is_you ? 'Give up your own access' : 'Revoke'}
                  onClick={() =>
                    void act(
                      async () => {
                        await api.removeShare(kind, id, share.id)
                        // Removing your own access closes the dialog: the thing
                        // it is about is gone from under it.
                        if (share.is_you) onClose()
                      },
                      { reload: !share.is_you },
                    )
                  }
                >
                  {share.is_you ? 'Leave' : 'Remove'}
                </button>
              </div>
            ))
          )}
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
}
