import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Session as AuthSession } from '@supabase/supabase-js'
import { client, configure } from './lib/supabase'
import {
  api,
  canControl,
  checkedAgo,
  liveActivity,
  type Project,
  type Server,
  type Session,
} from './lib/api'
import { useResource } from './lib/useResource'
import { ProjectTerminal } from './components/Terminal'
import { Auth } from './routes/Auth'
import { Connect } from './routes/Connect'
import { setBadge } from './lib/notifications'
import {
  currentHost,
  fetchConfig,
  forgetHost,
  rememberHost,
  storedHost,
  type InstanceConfig,
} from './lib/host'
import { AddServer } from './routes/AddServer'
import { NewProject } from './routes/NewProject'
import { Settings } from './routes/Settings'
import { Ports } from './components/Ports'
import { Feed } from './components/Feed'
import { Share } from './components/Share'
import { SessionWindow } from './routes/SessionWindow'
import { openSessionWindow, sessionWindowUrl } from './lib/desktop'

export function App() {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [ready, setReady] = useState(false)
  // Null until we have asked a host who it is. Everything else waits on this,
  // because the auth client cannot be built without it.
  const [config, setConfig] = useState<InstanceConfig | null>(null)
  const [hostProblem, setHostProblem] = useState<string | null>(null)

  const attach = useCallback((next: InstanceConfig) => {
    const supabase = configure(next)
    setConfig(next)
    void supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      setReady(true)
    })
    supabase.auth.onAuthStateChange((_event, updated) => setSession(updated))
  }, [])

  useEffect(() => {
    if (config) return
    // The host we were served from is right whenever the API serves the app,
    // so a fresh install usually needs no setup at all. Asking is the fallback.
    let cancelled = false
    void fetchConfig(currentHost())
      .then((found) => {
        if (!cancelled) attach(found)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setHostProblem(storedHost() ? String(err instanceof Error ? err.message : err) : null)
        setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [config, attach])

  if (!config) {
    if (!ready) return <div className="auth-shell">Connecting…</div>
    return (
      <Connect
        initial={storedHost() ?? undefined}
        problem={hostProblem}
        onConnected={(host, found) => {
          rememberHost(host)
          setHostProblem(null)
          attach(found)
        }}
      />
    )
  }

  // Opening the app is the acknowledgement: whatever was waiting has now been
  // seen, so the icon should stop claiming otherwise.
  useEffect(() => {
    if (!session) return
    void setBadge(0)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void setBadge(0)
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [session])

  if (!ready) return <div className="auth-shell">Loading…</div>
  if (!session) return <Auth />

  // A session opened in its own window renders only that session. Same app,
  // same components, no second implementation — the window is just a different
  // entry point, which is also what makes it work as a plain browser popup.
  const params = new URLSearchParams(window.location.search)
  if (params.get('window') === 'session') {
    const projectId = params.get('project')
    const name = params.get('session')
    if (projectId && name) {
      return <SessionWindow projectId={projectId} session={name} />
    }
  }

  return (
    <Shell
      email={session.user.email ?? ''}
      onDisconnect={() => {
        forgetHost()
        window.location.reload()
      }}
    />
  )
}

type ShareTarget = { kind: 'servers' | 'projects'; id: string; name: string }

function Shell({ email, onDisconnect }: { email: string; onDisconnect: () => void }) {
  // A session is part of the selection rather than a tab inside a project:
  // it is the thing you are actually looking at, so it belongs in the place
  // that says what you are looking at.
  const [selected, setSelected] = useState<
    { kind: 'server'; id: string } | { kind: 'project'; id: string; session?: string } | null
  >(null)
  const [showAddServer, setShowAddServer] = useState(false)
  const [showNewProject, setShowNewProject] = useState(false)
  const [showSidebar, setShowSidebar] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)

  // Servers are polled: bootstrap and Docker installs finish out of band, and
  // a stale "bootstrapping" chip is the most confusing thing the UI can show.
  const servers = useResource<Server[]>(() => api.servers(), [], { pollMs: 15000 })
  const projects = useResource<Project[]>(() => api.projects(), [], { pollMs: 8000 })
  const harnesses = useResource(() => api.harnesses(), [])
  const profile = useResource(() => api.profile(), [])
  const environments = useResource(() => api.environments(), [])
  // Every session, in one query and without touching a server. Listing what
  // exists must not cost a connection to a machine that may be asleep.
  const sessions = useResource(() => api.allSessions(), [], { pollMs: 10000 })

  const reloadAll = useCallback(() => {
    void servers.reload(true)
    void projects.reload(true)
    void profile.reload(true)
    // Signing in changes which harnesses are usable, so this must refresh too.
    void harnesses.reload(true)
    void sessions.reload(true)
  }, [servers, projects, profile, harnesses, sessions])

  const selectProject = useCallback((id: string, session?: string) => {
    setSelected({ kind: 'project', id, session })
    setShowSidebar(false)
  }, [])

  const activeProject =
    selected?.kind === 'project'
      ? (projects.data?.find((p) => p.id === selected.id) ?? null)
      : null
  const activeServer =
    selected?.kind === 'server'
      ? (servers.data?.find((s) => s.id === selected.id) ?? null)
      : null

  // A project shared with you directly usually sits on a machine you cannot
  // see at all, so it has nowhere to nest. Giving those their own group is
  // also the honest picture: they are not part of your infrastructure.
  const { grouped, loose } = useMemo(() => {
    const visible = new Set((servers.data ?? []).map((s) => s.id))
    const all = projects.data ?? []
    return {
      grouped: all.filter((p) => visible.has(p.server_id)),
      loose: all.filter((p) => !visible.has(p.server_id)),
    }
  }, [servers.data, projects.data])

  return (
    <div className={`app${showSidebar ? ' show-sidebar' : ''}`}>
      <aside className="sidebar">
        <div className="brand">
          <span className="glyph">◐</span> Moonphase
        </div>

        <div className="sidebar-scroll">
          <div className="section-label">
            Servers
            <button className="ghost" onClick={() => setShowAddServer(true)} title="Add server">
              +
            </button>
          </div>

          {servers.error && <div className="banner error">{servers.error}</div>}
          {servers.data?.length === 0 && loose.length === 0 && (
            <p className="hint" style={{ padding: '4px 8px' }}>
              No servers yet.
            </p>
          )}

          {servers.data?.map((server) => (
            <div className="tree-server" key={server.id}>
              <button
                className={`tree-row status-${server.status}${
                  activeServer?.id === server.id ? ' active' : ''
                }`}
                onClick={() => {
                  setSelected({ kind: 'server', id: server.id })
                  setShowSidebar(false)
                }}
              >
                <span className="dot" />
                <span className="name">{server.name}</span>
                {server.shared && (
                  <span className="shared-tag" title="Shared with you">
                    shared
                  </span>
                )}
              </button>

              {grouped
                .filter((p) => p.server_id === server.id)
                .map((project) => (
                  <ProjectRow
                    key={project.id}
                    project={project}
                    active={activeProject?.id === project.id}
                    activeSession={
                      selected?.kind === 'project' && selected.id === project.id
                        ? selected.session
                        : undefined
                    }
                    sessions={(sessions.data ?? []).filter(
                      (s) => s.project_id === project.id,
                    )}
                    onSelect={selectProject}
                  />
                ))}
            </div>
          ))}

          {loose.length > 0 && (
            <>
              <div className="section-label" style={{ marginTop: 12 }}>
                Shared with you
              </div>
              {loose.map((project) => (
                <ProjectRow
                  key={project.id}
                  project={project}
                  active={activeProject?.id === project.id}
                  activeSession={
                    selected?.kind === 'project' && selected.id === project.id
                      ? selected.session
                      : undefined
                  }
                  sessions={(sessions.data ?? []).filter((s) => s.project_id === project.id)}
                  onSelect={selectProject}
                  subtitle={project.server_name ?? undefined}
                />
              ))}
            </>
          )}

          <div className="section-label" style={{ marginTop: 12 }}>
            <button
              className="ghost"
              onClick={() => setShowNewProject(true)}
              disabled={
                !servers.data?.some((s) => s.status === 'online' && canControl(s.access))
              }
            >
              + New project
            </button>
          </div>
        </div>

        <div className="sidebar-foot">
          <span className="who" title={email}>
            {email}
          </span>
          <button className="ghost" onClick={() => setShowSettings(true)} title="Settings">
            Settings
          </button>
          <button className="ghost" onClick={() => void client().auth.signOut()}>
            Sign out
          </button>
          <button className="ghost" onClick={onDisconnect} title={currentHost()}>
            Host
          </button>
        </div>
      </aside>

      <main className="main">
        {activeProject ? (
          <ProjectView
            project={activeProject}
            session={selected?.kind === 'project' ? (selected.session ?? null) : null}
            sessions={(sessions.data ?? []).filter((s) => s.project_id === activeProject.id)}
            onEnter={(name) => selectProject(activeProject.id, name)}
            onChanged={reloadAll}
            onToggleSidebar={() => setShowSidebar(true)}
            onShare={() =>
              setShareTarget({
                kind: 'projects',
                id: activeProject.id,
                name: activeProject.name,
              })
            }
            onRemoved={() => {
              setSelected(null)
              reloadAll()
            }}
          />
        ) : activeServer ? (
          <ServerView
            server={activeServer}
            onChanged={reloadAll}
            onNewProject={() => setShowNewProject(true)}
            onToggleSidebar={() => setShowSidebar(true)}
            onShare={() =>
              setShareTarget({
                kind: 'servers',
                id: activeServer.id,
                name: activeServer.name,
              })
            }
          />
        ) : (
          <>
            <div className="topbar">
              <button className="ghost" onClick={() => setShowSidebar(true)}>
                ☰
              </button>
              <h1>Moonphase</h1>
            </div>
            <div className="content">
              <div className="empty">
                <h3>Nothing selected</h3>
                Add a server, create a project, and it keeps running
                <br />
                whether or not this window is open.
              </div>
            </div>
          </>
        )}
      </main>

      {showSettings && (
        <Settings onClose={() => setShowSettings(false)} onSaved={reloadAll} />
      )}
      {showAddServer && (
        <AddServer onClose={() => setShowAddServer(false)} onCreated={reloadAll} />
      )}
      {shareTarget && (
        <Share
          kind={shareTarget.kind}
          id={shareTarget.id}
          name={shareTarget.name}
          onClose={() => setShareTarget(null)}
          onChanged={reloadAll}
        />
      )}
      {showNewProject && (
        <NewProject
          servers={(servers.data ?? []).filter((s) => canControl(s.access))}
          harnesses={harnesses.data ?? []}
          environments={environments.data ?? []}
          defaultServerId={activeServer?.id ?? activeProject?.server_id}
          onOpenSettings={() => {
            setShowNewProject(false)
            setShowSettings(true)
          }}
          onClose={() => setShowNewProject(false)}
          onCreated={(id) => {
            reloadAll()
            selectProject(id)
          }}
        />
      )}
    </div>
  )
}

function ProjectRow({
  project,
  active,
  activeSession,
  sessions,
  onSelect,
  subtitle,
}: {
  project: Project
  active: boolean
  activeSession?: string
  sessions: Session[]
  onSelect: (id: string, session?: string) => void
  subtitle?: string
}) {
  return (
    <>
      <button
        // One class, one meaning. It used to carry both `status-*` and
        // `activity-*`, which style the same dot with different vocabularies —
        // whichever rule came later in the stylesheet won, so a container
        // mid-build showed its session's colour rather than its own.
        className={`tree-row tree-project ${
          project.status === 'running'
            ? `activity-${liveActivity(project)}`
            : `status-${project.status}`
        }${active && !activeSession ? ' active' : ''}`}
        onClick={() => onSelect(project.id)}
        title={project.activity_detail ?? subtitle}
      >
        <span className="dot" />
        <span className="name">
          {project.name}
          {subtitle && <span className="tree-sub">{subtitle}</span>}
        </span>
        {project.access === 'read' && (
          <span className="shared-tag" title="View only">
            view
          </span>
        )}
        {project.access === 'host' && (
          <span className="shared-tag" title="Runs on your server; not yours">
            guest
          </span>
        )}
        {project.status === 'running' && liveActivity(project) === 'awaiting_input' && (
          <span className="needs-you" title="Waiting for you">
            ●
          </span>
        )}
      </button>

      {/* Sessions belong here rather than in a tab strip: a session is the
          thing you are actually looking at, so it should be navigable in the
          same place as everything else, and several projects' sessions should
          be visible at once. Nothing connects until one is opened. */}
      {project.status === 'running' &&
        sessions.map((session) => (
          <button
            key={session.id}
            className={`tree-row tree-session activity-${liveActivity(session)}${
              active && activeSession === session.tmux_session ? ' active' : ''
            }`}
            onClick={() => onSelect(project.id, session.tmux_session)}
            title={[
              session.is_mine ? null : `${session.owner ?? 'Someone else'}'s session`,
              session.activity_detail ?? session.branch,
              checkedAgo(session),
            ]
              .filter(Boolean)
              .join(' · ')}
          >
            <span className="dot" />
            <span className="name">{session.tmux_session}</span>
            {!session.is_mine && (
              <span className="session-theirs">
                {session.owner?.split('@')[0] ?? 'shared'}
              </span>
            )}
            {liveActivity(session) === 'awaiting_input' && (
              <span className="needs-you" title="Waiting for you">
                ●
              </span>
            )}
          </button>
        ))}
    </>
  )
}

function ProjectView({
  project,
  session,
  sessions,
  onEnter,
  onChanged,
  onToggleSidebar,
  onShare,
  onRemoved,
}: {
  project: Project
  /** Null until a session is entered — and nothing connects before that. */
  session: string | null
  sessions: Session[]
  onEnter: (name: string) => void
  onChanged: () => void
  onToggleSidebar: () => void
  onShare: () => void
  onRemoved: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const active = sessions.find((s) => s.tmux_session === session) ?? null
  // Set briefly when a keystroke is refused, so the explanation reacts to the
  // attempt instead of sitting there having already been read and dismissed.
  const [nudged, setNudged] = useState(false)
  // A terminal is unusable on a phone, and attaching one would also drag the
  // desktop's tmux window down to phone width. Default by screen size, but
  // leave it switchable: the feed is genuinely nicer for catching up, and the
  // terminal is still the only way to do anything unusual.
  const [view, setView] = useState<'terminal' | 'feed'>(() =>
    typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches
      ? 'feed'
      : 'terminal',
  )

  const nudge = useCallback(() => {
    setNudged(true)
    window.setTimeout(() => setNudged(false), 1200)
  }, [])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // Two conditions, and they are different questions. Project access decides
  // whether you may run anything here at all; session ownership decides whether
  // this particular agent is yours to type into.
  //
  // Note which way the unknown case falls. Until the session list resolves we
  // do not know whose session this is, and treating that as read-only meant
  // every keystroke in the first second after opening a project — and after
  // any failed refresh — was swallowed by the client with no explanation. The
  // server is the gate and enforces ownership on every byte; this flag only
  // decides whether to explain. So it fails open, and closes only once we
  // positively know the session belongs to someone else.
  const watching = active !== null && !active.is_mine
  const drivable = canControl(project.access) && !watching

  // Someone else's project on your machine. You are told it is there and can
  // take the resources back; the conversation is not yours to read.
  if (project.access === 'host') {
    return (
      <>
        <div className="topbar">
          <button className="ghost" onClick={onToggleSidebar}>
            ☰
          </button>
          <h1>{project.name}</h1>
          <span className="sub">{project.server_name}</span>
          <span className="shared-tag">guest</span>
          <div className="spacer" />
        </div>
        <div className="content">
          {error && <div className="banner error">{error}</div>}
          <div className="card">
            <h2>Running on your server</h2>
            <p className="hint">
              Someone you shared <strong>{project.server_name}</strong> with created this
              project. It is theirs: Moonphase will not show you its terminal, its feed or
              its transcript.
            </p>
            <dl className="meta-grid">
              <dt>Status</dt>
              <dd className={`status-${project.status}`}>{project.status}</dd>
              <dt>Environment</dt>
              <dd>{project.environment}</dd>
              <dt>Created</dt>
              <dd>{new Date(project.created_at).toLocaleString()}</dd>
            </dl>
          </div>
          <div className="card">
            <h2>Reclaim</h2>
            <p className="hint">
              Removing it stops the container and frees its resources on your machine. The
              volumes are kept, so the work itself survives. To stop new projects appearing,
              revoke the server share instead.
            </p>
            <button
              className="danger"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  await api.deleteProject(project.id)
                  onRemoved()
                })
              }
            >
              Remove project
            </button>
          </div>
        </div>
      </>
    )
  }

  return (
    <>
      <div className="topbar">
        <button className="ghost" onClick={onToggleSidebar}>
          ☰
        </button>
        <h1>{project.name}</h1>
        <span className="sub">
          {project.server_name} · {project.environment}
        </span>
        {project.status === 'running' && <ActivityChip project={project} />}
        {watching ? (
          <span
            className="shared-tag"
            title={`This session runs on ${active?.owner ?? 'someone else'}'s account, so only they can type into it`}
          >
            watching {active?.owner?.split('@')[0] ?? 'someone else'}
          </span>
        ) : (
          active?.branch && <span className="shared-tag">{active.branch}</span>
        )}
        <div className="spacer" />
        <div className="view-toggle" role="group" aria-label="View">
          <button
            className={view === 'feed' ? 'active' : ''}
            onClick={() => setView('feed')}
            title="Readable feed — works on a phone, and never resizes the terminal"
          >
            Feed
          </button>
          <button
            className={view === 'terminal' ? 'active' : ''}
            onClick={() => setView('terminal')}
            title="The real terminal"
          >
            Terminal
          </button>
        </div>
        {project.access === 'admin' && (
          <button onClick={onShare} title="Give someone else access to this session">
            Share{project.share_count > 0 ? ` (${project.share_count})` : ''}
          </button>
        )}
        {session && (
          <button
            disabled={busy}
            title="Open this session in its own window — one per monitor, tiled by your window manager"
            onClick={() =>
              void act(() =>
                openSessionWindow({
                  projectId: project.id,
                  session,
                  title: `${session} — ${project.name}`,
                  url: sessionWindowUrl(project.id, session),
                }),
              )
            }
          >
            Window
          </button>
        )}
        {drivable && (
          <button
            disabled={busy}
            onClick={() => void act(() => api.startSession(project.id, true, session ?? undefined))}
            title="Kill this session and start the harness fresh"
          >
            Restart harness
          </button>
        )}
        {drivable &&
          (project.status === 'running' ? (
            <button disabled={busy} onClick={() => void act(() => api.stopProject(project.id))}>
              Stop
            </button>
          ) : (
            <button
              className="primary"
              disabled={busy}
              onClick={() => void act(() => api.startProject(project.id))}
            >
              Start
            </button>
          ))}
      </div>

      {error && (
        <div style={{ padding: '10px 16px 0' }}>
          <div className="banner error">{error}</div>
        </div>
      )}

      {project.status !== 'running' ? (
        <div className="content">
          <div className="empty">
            <h3>Container is {project.status}</h3>
            {project.status_detail ??
              (canControl(project.access)
                ? 'Start it to open a session.'
                : 'Whoever owns this project needs to start it.')}
          </div>
        </div>
      ) : !session ? (
        // No session entered, so nothing is connected. Opening a project used
        // to attach a terminal and a feed immediately, which cost a channel
        // and a tmux client before anyone had asked to look at anything.
        <div className="content">
          <div className="card">
            <h2>Sessions</h2>
            <p className="hint">
              A session is one person&rsquo;s agent — their account, their branch, their
              commits. Open one to connect; nothing is attached until you do.
            </p>
            {sessions.length === 0 ? (
              <p className="hint">Nothing running here yet.</p>
            ) : (
              <div className="session-list">
                {sessions.map((item) => (
                  <div className="session-card" key={item.id}>
                    <button className="session-enter" onClick={() => onEnter(item.tmux_session)}>
                      <span className={`dot activity-${liveActivity(item)}`} />
                      <span className="session-name">{item.tmux_session}</span>
                      <span className="session-meta">
                        {item.is_mine ? 'yours' : (item.owner ?? 'someone else')}
                        {item.branch ? ` · ${item.branch}` : ''}
                      </span>
                      {liveActivity(item) === 'awaiting_input' && (
                        <span className="needs-you" title="Waiting for you">
                          ●
                        </span>
                      )}
                    </button>
                    <button
                      className="ghost"
                      title="Open in its own window — one per monitor, tiled by your window manager"
                      onClick={() =>
                        void act(() =>
                          openSessionWindow({
                            projectId: project.id,
                            session: item.tmux_session,
                            title: `${item.tmux_session} — ${project.name}`,
                            url: sessionWindowUrl(project.id, item.tmux_session),
                          }),
                        )
                      }
                    >
                      window
                    </button>
                  </div>
                ))}
              </div>
            )}
            {canControl(project.access) && !sessions.some((s) => s.is_mine) && (
              <button
                className="primary"
                disabled={busy}
                style={{ marginTop: 12 }}
                onClick={() =>
                  void act(async () => {
                    const created = await api.createSession(project.id)
                    onEnter(created.tmux_session)
                  })
                }
              >
                Start my session
              </button>
            )}
          </div>
          <Ports
            projectId={project.id}
            projectName={project.name}
            running
            readOnly={!canControl(project.access)}
          />
        </div>
      ) : (
        <div className="content flush terminal-and-ports">
          {watching && (
            <div className={`readonly-bar${nudged ? ' nudged' : ''}`}>
              <span className="readonly-chip">Read-only</span>
              <span className="readonly-text">
                This is <strong>{active?.owner ?? 'someone else'}</strong>&rsquo;s session.
                It runs on their Claude account, so only they can type into it — you can
                watch it live.
              </span>
              {canControl(project.access) && (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const created = await api.createSession(project.id)
                      onEnter(created.tmux_session)
                    })
                  }
                >
                  Start my own session
                </button>
              )}
            </div>
          )}
          {view === 'terminal' ? (
            <ProjectTerminal
              projectId={project.id}
              session={session}
              readOnly={!drivable}
              onRefusedInput={nudge}
            />
          ) : (
            <Feed
              projectId={project.id}
              session={session}
              running
              readOnly={!drivable}
              onRefusedInput={nudge}
            />
          )}
          <Ports
            projectId={project.id}
            projectName={project.name}
            running
            readOnly={!drivable}
          />
        </div>
      )}
    </>
  )
}

const ACTIVITY_LABEL: Record<string, string> = {
  working: 'working',
  awaiting_input: 'waiting for you',
  idle: 'idle',
  stopped: 'stopped',
  unknown: '',
}

function ActivityChip({ project }: { project: Project }) {
  const state = liveActivity(project)
  const label = ACTIVITY_LABEL[state] ?? ''
  if (!label) return null
  return (
    <span
      className={`activity-chip activity-${state}`}
      title={[project.activity_detail, checkedAgo(project)].filter(Boolean).join(' · ')}
    >
      <span className="dot" />
      {label}
    </span>
  )
}

function ServerView({
  server,
  onChanged,
  onNewProject,
  onToggleSidebar,
  onShare,
}: {
  server: Server
  onChanged: () => void
  onNewProject: () => void
  onToggleSidebar: () => void
  onShare: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const owned = server.access === 'admin'

  return (
    <>
      <div className="topbar">
        <button className="ghost" onClick={onToggleSidebar}>
          ☰
        </button>
        <h1>{server.name}</h1>
        <span className="sub">
          {server.ssh_user}@{server.host}:{server.port}
        </span>
        {server.shared && (
          <span className="shared-tag" title="Shared with you by its owner">
            shared with you
          </span>
        )}
        <div className="spacer" />
        {owned && (
          <>
            <button onClick={onShare} title="Let someone else run work on this machine">
              Share{server.share_count > 0 ? ` (${server.share_count})` : ''}
            </button>
            <button disabled={busy} onClick={() => void act(() => api.testServer(server.id))}>
              Test
            </button>
            <button
              disabled={busy || server.status !== 'online'}
              onClick={() => void act(() => api.bootstrapServer(server.id))}
            >
              Re-bootstrap
            </button>
          </>
        )}
        <button
          className="primary"
          disabled={server.status !== 'online' || !canControl(server.access)}
          onClick={onNewProject}
        >
          New project
        </button>
      </div>

      <div className="content">
        {error && <div className="banner error">{error}</div>}
        {server.status_detail && (
          <div className={`banner ${server.status === 'online' ? 'info' : 'error'}`}>
            {server.status_detail}
          </div>
        )}

        <div className="card">
          <h2>Server</h2>
          <dl className="meta-grid">
            <dt>Status</dt>
            <dd className={`status-${server.status}`}>{server.status}</dd>
            <dt>Docker</dt>
            <dd>{server.docker_version ?? '—'}</dd>
            <dt>SSH auth</dt>
            <dd>{server.ssh_auth_mode}</dd>
            <dt>Host key</dt>
            <dd>{server.host_key_fingerprint ?? '—'}</dd>
            <dt>Projects</dt>
            <dd>{server.project_count}</dd>
            <dt>Last seen</dt>
            <dd>
              {server.last_seen_at ? new Date(server.last_seen_at).toLocaleString() : 'never'}
            </dd>
          </dl>
        </div>

        {!owned && (
          <div className="card">
            <h2>Shared with you</h2>
            <p className="hint">
              {canControl(server.access)
                ? 'You can create projects here. They belong to you — their owner sees that they exist and how much of the machine they are using, not what they are doing. Only the owner can administer the machine itself.'
                : 'You can see this machine but not run anything on it. Ask its owner for access if you need to.'}
            </p>
          </div>
        )}

        {owned && server.managed_public_key && (
          <div className="card">
            <h2>Moonphase public key</h2>
            <p className="hint">
              Installed in <code>~/.ssh/authorized_keys</code> for{' '}
              <code>{server.ssh_user}</code>. Removing this line revokes Moonphase's access
              to this server and nothing else.
            </p>
            <div className="keyblock">{server.managed_public_key}</div>
          </div>
        )}

        {owned && (
          <div className="card">
            <h2>Danger zone</h2>
            <p className="hint">
              Removing the server deletes its projects from Moonphase and revokes the key
              above. Container volumes on the machine are left alone.
              {server.share_count > 0 &&
                ` ${server.share_count} ${
                  server.share_count === 1 ? 'person' : 'people'
                } will lose access.`}
            </p>
            <button
              className="danger"
              disabled={busy}
              onClick={() => void act(() => api.deleteServer(server.id))}
            >
              Remove server
            </button>
          </div>
        )}
      </div>
    </>
  )
}
