import { useCallback, useEffect, useState } from 'react'
import { api, canControl, type Project, type Session } from '../lib/api'
import { useResource } from '../lib/useResource'
import { ProjectTerminal } from '../components/Terminal'
import { Feed } from '../components/Feed'

interface Props {
  projectId: string
  session: string
}

/**
 * One session, alone in a window.
 *
 * People run several agents at once and want them side by side, which is a
 * layout problem an operating system already solves. So rather than build
 * panes and splitters, a session can be given a window of its own — a tiling
 * window manager arranges those better than we could, across however many
 * monitors there are, and the same URL works as a plain browser popup.
 *
 * It renders the same terminal and feed the main window does. Nothing here is
 * a second implementation.
 */
export function SessionWindow({ projectId, session }: Props) {
  const [view, setView] = useState<'terminal' | 'feed'>('terminal')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const project = useResource<Project>(() => api.project(projectId), [projectId], {
    pollMs: 15000,
  })
  const sessions = useResource<Session[]>(
    () => api.sessions(projectId, true),
    [projectId],
    { pollMs: 10000 },
  )

  const mine = sessions.data?.find((s) => s.tmux_session === session) ?? null
  const drivable = Boolean(project.data && canControl(project.data.access) && mine?.is_mine)
  const watching = mine !== null && !mine.is_mine

  const label = mine?.display_name ?? session

  useEffect(() => {
    document.title = project.data ? `${label} — ${project.data.name}` : `${label} — Moonphase`
  }, [project.data, label])

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true)
      setError(null)
      try {
        await fn()
        await sessions.reload(true)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setBusy(false)
      }
    },
    [sessions],
  )

  if (project.error && !project.data) {
    return <div className="auth-shell">{project.error}</div>
  }
  if (!project.data) return <div className="auth-shell">Loading…</div>

  const running = project.data.status === 'running'

  return (
    <div className="session-window">
      <div className="topbar">
        <h1>{label}</h1>
        <span className="sub">
          {project.data.name} · {project.data.server_name}
        </span>
        {mine?.branch && <span className="shared-tag">{mine.branch}</span>}
        {watching && (
          <span className="shared-tag" title="Runs on their account, so only they can type">
            watching {mine?.owner?.split('@')[0] ?? 'someone else'}
          </span>
        )}
        <div className="spacer" />
        <div className="view-toggle" role="group" aria-label="View">
          <button className={view === 'feed' ? 'active' : ''} onClick={() => setView('feed')}>
            Feed
          </button>
          <button
            className={view === 'terminal' ? 'active' : ''}
            onClick={() => setView('terminal')}
          >
            Terminal
          </button>
        </div>
        {drivable && (
          <button
            disabled={busy}
            onClick={() => void act(() => api.startSession(projectId, true, session))}
            title="Kill this session and start the harness fresh"
          >
            Restart
          </button>
        )}
      </div>

      {error && (
        <div style={{ padding: '10px 16px 0' }}>
          <div className="banner error">{error}</div>
        </div>
      )}

      {!running ? (
        <div className="content">
          <div className="empty">
            <h3>Project is {project.data.status}</h3>
            Start it from the main window to attach.
          </div>
        </div>
      ) : (
        <div className="content flush terminal-and-ports">
          {view === 'terminal' ? (
            <ProjectTerminal projectId={projectId} session={session} readOnly={!drivable} />
          ) : (
            <Feed
              projectId={projectId}
              session={session}
              running={running}
              readOnly={!drivable}
            />
          )}
        </div>
      )}
    </div>
  )
}
