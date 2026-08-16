import { useCallback, useEffect, useState } from 'react'
import { api, type Session } from '../lib/api'

interface Props {
  projectId: string
  running: boolean
  active: string
  onSelect: (session: string) => void
  /** Shared with view-only access: pick a session to watch, change nothing. */
  readOnly?: boolean
  /** Whichever session is selected, so the view above knows whose it is. */
  onActiveSession?: (session: Session | null) => void
}

const ACTIVITY_TITLE: Record<string, string> = {
  working: 'working',
  awaiting_input: 'waiting for you',
  idle: 'idle',
  stopped: 'not running',
  unknown: '',
}

/**
 * Sessions within a project, as tabs.
 *
 * A session is one person's agent: their Claude account, their git identity,
 * their branch. Several run side by side in one project, and you may watch any
 * of them but type only into your own — sharing a project shares the code, not
 * the subscription behind it.
 *
 * Yours come first, and someone else's is marked with their name and opens
 * read-only, so the missing keyboard is explained rather than mysterious.
 */
export function Sessions({
  projectId,
  running,
  active,
  onSelect,
  readOnly = false,
  onActiveSession,
}: Props) {
  const [items, setItems] = useState<Session[]>([])
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const next = await api.sessions(projectId)
      setItems(next)
      setError(null)
      // If the selected session disappeared, fall back rather than leaving
      // the terminal pointed at nothing.
      if (next.length > 0 && !next.some((s) => s.tmux_session === active)) {
        onSelect(next[0].tmux_session)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [projectId, active, onSelect])

  useEffect(() => {
    void load()
    if (!running) return
    const id = window.setInterval(() => void load(), 10000)
    return () => window.clearInterval(id)
  }, [load, running])

  // Switching tabs detaches the previous session's client, but that happens on
  // the server a moment after the switch. Without a follow-up read the old tab
  // keeps advertising a device that has already gone, for a full poll interval.
  useEffect(() => {
    const id = window.setTimeout(() => void load(), 2000)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // Whether the view above should offer a keyboard depends on whose session
  // this is, which only this component knows.
  useEffect(() => {
    onActiveSession?.(items.find((s) => s.tmux_session === active) ?? null)
  }, [items, active, onActiveSession])

  const create = async () => {
    setBusy(true)
    setError(null)
    try {
      const created = await api.createSession(projectId, name.trim() || undefined)
      setName('')
      setAdding(false)
      await load()
      onSelect(created.tmux_session)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (session: Session) => {
    setBusy(true)
    setError(null)
    try {
      await api.deleteSession(projectId, session.tmux_session)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const detach = async (session: Session) => {
    setBusy(true)
    setError(null)
    try {
      await api.detachClients(projectId, session.tmux_session)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (!running) return null

  return (
    <div className="sessions">
      <div className="session-tabs">
        {items.map((session) => {
          const isActive = session.tmux_session === active
          return (
            <div
              key={session.id}
              className={`session-tab activity-${session.activity}${
                isActive ? ' active' : ''
              }`}
            >
              <button
                className="session-open"
                onClick={() => onSelect(session.tmux_session)}
                title={
                  session.is_mine
                    ? (session.activity_detail ?? ACTIVITY_TITLE[session.activity])
                    : `${session.owner ?? 'Someone else'}'s session${
                        session.branch ? ` on ${session.branch}` : ''
                      } — you can watch it, not type into it`
                }
              >
                <span className="dot" />
                {session.tmux_session}
                {!session.is_mine && (
                  <span className="session-theirs" aria-hidden="true">
                    ◦
                  </span>
                )}
                {session.attached_clients > 1 && (
                  <span
                    className="session-clients"
                    title={`${session.attached_clients} devices viewing this session`}
                  >
                    {session.attached_clients}
                  </span>
                )}
              </button>
              {items.length > 1 && !readOnly && session.is_mine && (
                <button
                  className="session-close"
                  disabled={busy}
                  title={`Delete ${session.tmux_session}`}
                  onClick={() => void remove(session)}
                >
                  ×
                </button>
              )}
            </div>
          )
        })}

        {readOnly ? null : adding ? (
          <form
            className="session-new"
            onSubmit={(e) => {
              e.preventDefault()
              void create()
            }}
          >
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="name (optional)"
              autoFocus
              onBlur={() => !name.trim() && setAdding(false)}
            />
            <button className="primary" type="submit" disabled={busy}>
              Add
            </button>
          </form>
        ) : (
          <button
            className="session-add"
            onClick={() => setAdding(true)}
            title="Start a session of your own, on its own branch"
          >
            +
          </button>
        )}

        <div className="spacer" />

        {!readOnly &&
          items.find(
            (s) => s.tmux_session === active && s.is_mine && s.attached_clients > 1,
          ) && (
            <button
              className="ghost session-detach"
              disabled={busy}
              title="Detach every device from this session. The session keeps running."
              onClick={() => {
                const current = items.find((s) => s.tmux_session === active)
                if (current) void detach(current)
              }}
            >
              Detach others
            </button>
          )}
      </div>

      {error && <div className="session-error">{error}</div>}
    </div>
  )
}
