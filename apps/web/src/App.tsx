import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Session as AuthSession } from '@supabase/supabase-js'
import { supabase } from './lib/supabase'
import { api, canControl, type Project, type Server, type Session } from './lib/api'
import { useResource } from './lib/useResource'
import { ProjectTerminal } from './components/Terminal'
import { Auth } from './routes/Auth'
import { AddServer } from './routes/AddServer'
import { NewProject } from './routes/NewProject'
import { Settings } from './routes/Settings'
import { Ports } from './components/Ports'
import { Sessions } from './components/Sessions'
import { Feed } from './components/Feed'
import { Share } from './components/Share'

export function App() {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    void supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      setReady(true)
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
    })
    return () => sub.subscription.unsubscribe()
  }, [])

  if (!ready) return <div className="auth-shell">Loading…</div>
  if (!session) return <Auth />
  return <Shell email={session.user.email ?? ''} />
}

type ShareTarget = { kind: 'servers' | 'projects'; id: string; name: string }

function Shell({ email }: { email: string }) {
  const [selected, setSelected] = useState<
    { kind: 'server' | 'project'; id: string } | null
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

  const reloadAll = useCallback(() => {
    void servers.reload(true)
    void projects.reload(true)
    void profile.reload(true)
    // Signing in changes which harnesses are usable, so this must refresh too.
    void harnesses.reload(true)
  }, [servers, projects, profile, harnesses])

  const selectProject = useCallback((id: string) => {
    setSelected({ kind: 'project', id })
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
          <button className="ghost" onClick={() => void supabase.auth.signOut()}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        {activeProject ? (
          <ProjectView
            project={activeProject}
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
  onSelect,
  subtitle,
}: {
  project: Project
  active: boolean
  onSelect: (id: string) => void
  subtitle?: string
}) {
  return (
    <button
      className={`tree-row tree-project status-${project.status} activity-${
        project.status === 'running' ? project.activity : 'stopped'
      }${active ? ' active' : ''}`}
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
      {project.status === 'running' && project.activity === 'awaiting_input' && (
        <span className="needs-you" title="Waiting for you">
          ●
        </span>
      )}
    </button>
  )
}

function ProjectView({
  project,
  onChanged,
  onToggleSidebar,
  onShare,
  onRemoved,
}: {
  project: Project
  onChanged: () => void
  onToggleSidebar: () => void
  onShare: () => void
  onRemoved: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [session, setSession] = useState('')
  const [active, setActive] = useState<Session | null>(null)
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

  // Switching project must not leave the tab selection from the previous one.
  useEffect(() => {
    setSession('')
    setActive(null)
  }, [project.id])

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
        {canControl(project.access) && !active && (
          <button
            className="primary"
            disabled={busy}
            onClick={() => void act(() => api.startSession(project.id))}
            title="Start a session of your own here, on its own branch"
          >
            Start my session
          </button>
        )}
        {drivable && (
          <button
            disabled={busy}
            onClick={() => void act(() => api.startSession(project.id, true, session))}
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

      {project.status === 'running' ? (
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
                      setSession(created.tmux_session)
                    })
                  }
                >
                  Start my own session
                </button>
              )}
            </div>
          )}
          <Sessions
            projectId={project.id}
            running={project.status === 'running'}
            active={session}
            onSelect={setSession}
            readOnly={!canControl(project.access)}
            onActiveSession={setActive}
          />
          {!session ? (
            // Nothing is attached until we know which session to attach to.
            // Attaching first and correcting afterwards meant every open cost
            // two attaches, and on a shared project the first one landed on
            // whatever the server considers the default — a flash of someone
            // else's terminal before switching to your own.
            <div className="terminal-wrap">
              <div className="empty">Finding your session…</div>
            </div>
          ) : view === 'terminal' ? (
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
              running={project.status === 'running'}
              readOnly={!drivable}
              onRefusedInput={nudge}
            />
          )}
          <Ports
            projectId={project.id}
            projectName={project.name}
            running={project.status === 'running'}
            readOnly={!drivable}
          />
        </div>
      ) : (
        <div className="content">
          <div className="empty">
            <h3>Container is {project.status}</h3>
            {project.status_detail ??
              (drivable
                ? 'Start it to attach a terminal.'
                : 'Whoever owns this project needs to start it.')}
          </div>
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
  const label = ACTIVITY_LABEL[project.activity] ?? ''
  if (!label) return null
  return (
    <span className={`activity-chip activity-${project.activity}`} title={project.activity_detail ?? undefined}>
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
