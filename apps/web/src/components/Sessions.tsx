import { useCallback, useEffect, useState } from 'react'
import { api, type Session } from '../lib/api'

interface Props {
  projectId: string
  running: boolean
  active: string
  onSelect: (session: string) => void
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
 * A project is one workspace; a session is one agent working in it. Several
 * can run at once and they share the same checkout — which is the point, and
 * also worth being explicit about, since it is not the isolation people
 * assume from separate tabs.
 */
export function Sessions({ projectId, running, active, onSelect }: Props) {
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

  const create = async () => {
    setBusy(true)
    setError(null)
    try {
      const created = await api.createSession(projectId, name.trim())
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
                title={session.activity_detail ?? ACTIVITY_TITLE[session.activity]}
              >
                <span className="dot" />
                {session.tmux_session}
                {session.attached_clients > 1 && (
                  <span
                    className="session-clients"
                    title={`${session.attached_clients} devices viewing this session`}
                  >
                    {session.attached_clients}
                  </span>
                )}
              </button>
              {items.length > 1 && (
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

        {adding ? (
          <form
            className="session-new"
            onSubmit={(e) => {
              e.preventDefault()
              if (name.trim()) void create()
            }}
          >
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="session name"
              autoFocus
              onBlur={() => !name.trim() && setAdding(false)}
            />
            <button className="primary" type="submit" disabled={busy || !name.trim()}>
              Add
            </button>
          </form>
        ) : (
          <button className="session-add" onClick={() => setAdding(true)} title="New session">
            +
          </button>
        )}

        <div className="spacer" />

        {items.find((s) => s.tmux_session === active && s.attached_clients > 1) && (
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
