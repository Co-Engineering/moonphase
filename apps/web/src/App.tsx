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
  setupState,
} from './lib/api'
import { useResource } from './lib/useResource'
import { Logo } from './components/Logo'
import { ProjectTerminal } from './components/Terminal'
import { Auth } from './routes/Auth'
import { Connect } from './routes/Connect'
import { Setup } from './routes/Setup'
import { setBadge } from './lib/notifications'
import {
  currentHost,
  fetchConfig,
  rememberHost,
  storedHost,
  type InstanceConfig,
} from './lib/host'
import { AddServer } from './routes/AddServer'
import { NewProject } from './routes/NewProject'
import { Settings } from './routes/Settings'
import { Usage, UsageStrip } from './components/Usage'
import { Changes } from './components/Changes'
import { ErrorBoundary } from './components/ErrorBoundary'
import { SavePoints } from './components/SavePoints'
import { Summary } from './components/Summary'
import { YourApp } from './components/YourApp'
import { Search } from './components/Search'
import { Feed } from './components/Feed'
import { Share } from './components/Share'
import { Attention, waiting } from './components/Attention'
import { SessionWindow } from './routes/SessionWindow'
import { openSessionWindow, sessionWindowUrl } from './lib/desktop'
import { HostDialog } from './components/HostDialog'
import { RowMenu } from './components/RowMenu'
import { RenameDialog } from './components/RenameDialog'
import { ClaudeConfigDialog } from './components/ClaudeConfigDialog'

export function App() {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [ready, setReady] = useState(false)
  // Null until we have asked a host who it is. Everything else waits on this,
  // because the auth client cannot be built without it.
  const [config, setConfig] = useState<InstanceConfig | null>(null)
  const [hostProblem, setHostProblem] = useState<string | null>(null)
  // Served by the instance, and the instance is not answering — during an
  // update, most likely. Waiting, rather than asking where it lives.
  const [reconnecting, setReconnecting] = useState(false)
  // Null until asked. An instance with no accounts shows setup rather than a
  // sign-in form nobody could satisfy.
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null)

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
    let attempt = 0

    // Being served by the instance means its address is not in question: the
    // page came from it. So a failed config fetch there is "it is unwell",
    // not "where is it?", and asking again is the only sensible answer.
    //
    // Presenting the host picker instead is what an update looked like from
    // the outside — the API is replaced, one request fails, and the app threw
    // away a working instance to show an empty box asking for an address the
    // person never typed and has no reason to know.
    const servedFromHost = !storedHost()

    const load = () => {
      void fetchConfig(currentHost())
        .then((found) => {
          if (!cancelled) attach(found)
        })
        .catch((err: unknown) => {
          if (cancelled) return
          if (servedFromHost) {
            attempt += 1
            setHostProblem(null)
            setReconnecting(true)
            // Backing off to five seconds: a restart is over in about that,
            // and hammering an instance that is coming up does not help it.
            window.setTimeout(load, Math.min(1000 * attempt, 5000))
            return
          }
          setHostProblem(String(err instanceof Error ? err.message : err))
          setReady(true)
        })
    }
    load()

    return () => {
      cancelled = true
    }
  }, [config, attach])

  // Asked once the host is known, and again after setup finishes. Kept above
  // every early return for the same reason as the effect below.
  useEffect(() => {
    if (!config) return
    let cancelled = false
    void setupState()
      .then((state) => {
        if (!cancelled) setNeedsSetup(state.needs_setup)
      })
      .catch(() => {
        // An instance too old to answer is an instance that is already set up.
        if (!cancelled) setNeedsSetup(false)
      })
    return () => {
      cancelled = true
    }
  }, [config])

  // Above every early return: a hook placed after one runs on some renders and
  // not others, and React tears the whole tree down when the count changes —
  // which shows up as a blank window with the error only in the console.
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

  if (!config) {
    if (reconnecting) {
      return (
        <div className="auth-shell">
          <p>Waiting for Moonphase to come back…</p>
          <p className="hint">
            The services are restarting, which is what an update looks like from
            here. This reconnects on its own.
          </p>
        </div>
      )
    }
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

  if (!ready) return <div className="auth-shell">Loading…</div>
  // Before anyone has an account, a sign-in form is a door with no key
  // behind it. Setup makes the first one, and it owns the instance.
  if (needsSetup === null) return <div className="auth-shell">Loading…</div>
  if (needsSetup) return <Setup onDone={() => setNeedsSetup(false)} />
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
    <Shell email={session.user.email ?? ''} />
  )
}

type ShareTarget = { kind: 'servers' | 'projects'; id: string; name: string }
type ConfigureTarget = { projectId: string; projectName: string; session?: string }

function Shell({ email }: { email: string }) {
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
  const [showHost, setShowHost] = useState(false)
  const [renaming, setRenaming] = useState<
    | { kind: 'server' | 'project'; id: string; name: string }
    // `id` is the tmux session name — the identifier `renameSession` needs
    // alongside the project it belongs to, not something being changed.
    | { kind: 'session'; id: string; name: string; projectId: string }
    | null
  >(null)
  const [showUsage, setShowUsage] = useState(false)
  const [showSearch, setShowSearch] = useState(false)

  // ⌘K / Ctrl-K. Registered here rather than inside the modal because its
  // whole job is opening the modal when it is not mounted.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setShowSearch(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)
  const [configureTarget, setConfigureTarget] = useState<ConfigureTarget | null>(null)

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

  // Closing a session you are currently looking at must drop you back at the
  // project rather than leaving `selected` pointing at a session that no
  // longer exists: the terminal view stayed up for a session that was gone,
  // with no way back to "New session" short of picking a different project
  // and back. Worse, reattaching in that state asked the server for a session
  // by that name again, and since a closed session's branch is deliberately
  // kept, the server quietly started a new one right back on it — so closing
  // your only session never actually let you start a clean one.
  const closeSession = useCallback(
    (projectId: string, tmuxSession: string) => {
      void api.deleteSession(projectId, tmuxSession).then(() => {
        setSelected((current) =>
          current?.kind === 'project' &&
          current.id === projectId &&
          current.session === tmuxSession
            ? { kind: 'project', id: projectId }
            : current,
        )
        reloadAll()
      })
    },
    [reloadAll],
  )

  // A notification names the thing that needs answering, and tapping it has to
  // land there. It arrives two ways: as query parameters when the app is
  // opened cold, and as a message from the service worker when a window is
  // already up.
  useEffect(() => {
    const open = (search: string) => {
      const params = new URLSearchParams(search)
      const project = params.get('project')
      if (!project) return
      selectProject(project, params.get('session') ?? undefined)
    }
    open(window.location.search)

    const onMessage = (event: MessageEvent) => {
      if (event.data?.type !== 'navigate' || typeof event.data.url !== 'string') return
      const url = new URL(event.data.url, window.location.origin)
      open(url.search)
    }
    navigator.serviceWorker?.addEventListener('message', onMessage)
    return () => navigator.serviceWorker?.removeEventListener('message', onMessage)
  }, [selectProject])

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
          <span className="glyph">
            <Logo />
          </span>{' '}
          Moonphase
        </div>

        <div className="sidebar-scroll">
          {waiting(sessions.data ?? []).length > 0 && (
            <button
              className="attention-chip"
              onClick={() => setSelected(null)}
              title="Sessions waiting for an answer"
            >
              <span className="dot activity-awaiting_input" />
              {waiting(sessions.data ?? []).length} waiting for you
            </button>
          )}
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
              {/* The server's own row, so hovering it reveals its menu and
                  not every menu in the block underneath it. */}
              <div className="tree-server-row">
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
                <RowMenu
                  label={server.name}
                  actions={[
                    ...(server.access === 'admin'
                      ? [
                          {
                            label: 'Share',
                            onSelect: () =>
                              setShareTarget({
                                kind: 'servers',
                                id: server.id,
                                name: server.name,
                              }),
                          },
                        ]
                      : []),
                    {
                      label: 'Rename',
                      disabledReason:
                        server.access === 'admin' ? undefined : 'not yours',
                      onSelect: () =>
                        setRenaming({
                          kind: 'server',
                          id: server.id,
                          name: server.name,
                        }),
                    },
                    {
                      label: 'Remove server',
                      danger: true,
                      detail:
                        'Deletes its projects from Moonphase and revokes the key. ' +
                        'Volumes on the machine are left alone.',
                      disabledReason:
                        server.access === 'admin' ? undefined : 'not yours',
                      onSelect: () =>
                        void api
                          .deleteServer(server.id)
                          .then(reloadAll)
                          .catch(() => reloadAll()),
                    },
                  ]}
                />
              </div>

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
                    onShare={(project) =>
                      setShareTarget({
                        kind: 'projects',
                        id: project.id,
                        name: project.name,
                      })
                    }
                    onRemove={(project) =>
                      void api.deleteProject(project.id).then(reloadAll)
                    }
                    onRename={(item) =>
                      setRenaming({
                        kind: 'project',
                        id: item.id,
                        name: item.name,
                      })
                    }
                    onRenameSession={(item) =>
                      setRenaming({
                        kind: 'session',
                        id: item.tmux_session,
                        name: item.display_name ?? item.tmux_session,
                        projectId: project.id,
                      })
                    }
                    onCloseSession={(item) => closeSession(project.id, item.tmux_session)}
                    onConfigure={(item) =>
                      setConfigureTarget({ projectId: item.id, projectName: item.name })
                    }
                    onConfigureSession={(item) =>
                      setConfigureTarget({
                        projectId: project.id,
                        projectName: project.name,
                        session: item.tmux_session,
                      })
                    }
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
                  onShare={(item) =>
                    setShareTarget({
                      kind: 'projects',
                      id: item.id,
                      name: item.name,
                    })
                  }
                  onRemove={(item) => void api.deleteProject(item.id).then(reloadAll)}
                  onRename={(item) =>
                    setRenaming({ kind: 'project', id: item.id, name: item.name })
                  }
                  onRenameSession={(item) =>
                    setRenaming({
                      kind: 'session',
                      id: item.tmux_session,
                      name: item.display_name ?? item.tmux_session,
                      projectId: project.id,
                    })
                  }
                  onCloseSession={(item) => closeSession(project.id, item.tmux_session)}
                  onConfigure={(item) =>
                    setConfigureTarget({ projectId: item.id, projectName: item.name })
                  }
                  onConfigureSession={(item) =>
                    setConfigureTarget({
                      projectId: project.id,
                      projectName: project.name,
                      session: item.tmux_session,
                    })
                  }
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
          <button
            className="ghost"
            onClick={() => setShowSearch(true)}
            title="Search every session you own (⌘K)"
          >
            Search
          </button>
          <button className="ghost" onClick={() => setShowUsage(true)} title="Token usage and spend">
            Usage
          </button>
          <button className="ghost" onClick={() => setShowSettings(true)} title="Settings">
            Settings
          </button>
          <button className="ghost" onClick={() => void client().auth.signOut()}>
            Sign out
          </button>
          <button
            className="ghost"
            onClick={() => setShowHost(true)}
            title={currentHost()}
          >
            Host
          </button>
        </div>
      </aside>

      <main className="main">
        {activeProject ? (
          <ErrorBoundary
            key={`${activeProject.id}:${selected?.kind === 'project' ? (selected.session ?? '') : ''}`}
            what="This session"
          >
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
          </ErrorBoundary>
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
              <Attention
                sessions={sessions.data ?? []}
                onOpen={(project, session) => selectProject(project, session)}
              />
              <UsageStrip onOpen={() => setShowUsage(true)} />
              {waiting(sessions.data ?? []).length === 0 && (
                <div className="empty">
                  <h3>Nothing is waiting for you</h3>
                  Close this and go somewhere. You will be told when an agent
                  <br />
                  needs an answer, whether or not this window is open.
                </div>
              )}
            </div>
          </>
        )}
      </main>

      {showHost && <HostDialog onClose={() => setShowHost(false)} />}

      {renaming && (
        <RenameDialog
          what={renaming.kind}
          current={renaming.name}
          note={
            renaming.kind === 'project'
              ? 'The display name only. The container and its volumes keep the name they were created with.'
              : renaming.kind === 'session'
                ? 'The display name only. Its home directory, worktree and branch keep the name they were created with.'
                : undefined
          }
          onRename={async (name) => {
            if (renaming.kind === 'server') await api.renameServer(renaming.id, name)
            else if (renaming.kind === 'session')
              await api.renameSession(renaming.projectId, renaming.id, name)
            else await api.renameProject(renaming.id, name)
            reloadAll()
          }}
          onClose={() => setRenaming(null)}
        />
      )}

      {showSettings && (
        <Settings onClose={() => setShowSettings(false)} onSaved={reloadAll} />
      )}
      {showUsage && <Usage onClose={() => setShowUsage(false)} />}
      {showSearch && (
        <Search
          onClose={() => setShowSearch(false)}
          onOpen={(project, session) => {
            setShowSearch(false)
            selectProject(project, session)
          }}
        />
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
      {configureTarget && (
        <ClaudeConfigDialog
          title={
            configureTarget.session
              ? `Configure ${configureTarget.session} — ${configureTarget.projectName}`
              : `Configure ${configureTarget.projectName}`
          }
          note={
            configureTarget.session
              ? 'Applies to this session only, on top of the project and your global settings.'
              : 'Applies to every session in this project, for everyone who can drive one.'
          }
          load={() =>
            configureTarget.session
              ? api.sessionConfig(configureTarget.projectId, configureTarget.session)
              : api.projectConfig(configureTarget.projectId)
          }
          save={(input) =>
            configureTarget.session
              ? api.saveSessionConfig(
                  configureTarget.projectId,
                  configureTarget.session,
                  input,
                )
              : api.saveProjectConfig(configureTarget.projectId, input)
          }
          onClose={() => setConfigureTarget(null)}
          // Connecting a server relays OAuth through a running session, so it
          // needs one — but a server is normally defined for the whole
          // project, and the project's own dialog has no session in hand.
          // Picking one is the backend's job (any of the caller's own running
          // sessions in this project will do, since the credential that comes
          // out is org-wide regardless), not something to guess at here from
          // whichever session list happens to be loaded and however fresh its
          // liveness is.
          mcpConnect={
            configureTarget.session
              ? {
                  scope: 'session',
                  projectId: configureTarget.projectId,
                  session: configureTarget.session,
                }
              : { scope: 'project', projectId: configureTarget.projectId }
          }
        />
      )}
      {showNewProject && (
        <NewProject
          servers={(servers.data ?? []).filter((s) => canControl(s.access))}
          harnesses={harnesses.data ?? []}
          environments={environments.data ?? []}
          profile={profile.data}
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
  onShare,
  onRemove,
  onRename,
  onRenameSession,
  onCloseSession,
  onConfigure,
  onConfigureSession,
}: {
  project: Project
  active: boolean
  activeSession?: string
  sessions: Session[]
  onSelect: (id: string, session?: string) => void
  subtitle?: string
  onShare?: (project: Project) => void
  onRemove?: (project: Project) => void
  onRename?: (project: Project) => void
  onRenameSession?: (session: Session) => void
  onCloseSession?: (session: Session) => void
  onConfigure?: (project: Project) => void
  onConfigureSession?: (session: Session) => void
}) {
  return (
    <>
      {/* The row and its menu together, so the menu is positioned
          against this row rather than against the server block it sits
          in — where every project's menu landed on the same few pixels,
          stacked on the server's own. Sessions have always been wrapped
          like this; projects were the one kind that was not. */}
      <div className="tree-project-row">
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
        <RowMenu
          label={project.name}
          actions={[
            ...(project.access === 'admin' && onShare
              ? [{ label: 'Share', onSelect: () => onShare(project) }]
              : []),
            {
              label: 'Rename',
              disabledReason: canControl(project.access) ? undefined : 'view only',
              onSelect: () => onRename?.(project),
            },
            {
              label: 'Configure',
              detail: 'MCP servers, skills, permissions and CLAUDE.md for this project.',
              disabledReason: canControl(project.access) ? undefined : 'view only',
              onSelect: () => onConfigure?.(project),
            },
            {
              label: 'Remove project',
              danger: true,
              detail:
                'Stops the container and removes the project. The volumes, and ' +
                'so the work in them, are kept.',
              disabledReason: canControl(project.access) ? undefined : 'view only',
              onSelect: () => onRemove?.(project),
            },
          ]}
        />
      </div>

      {/* Sessions belong here rather than in a tab strip: a session is the
          thing you are actually looking at, so it should be navigable in the
          same place as everything else, and several projects' sessions should
          be visible at once. Nothing connects until one is opened. */}
      {project.status === 'running' &&
        sessions.map((session) => (
          <div className="tree-session-row" key={session.id}>
          <button
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
            <span className="name">{session.display_name ?? session.tmux_session}</span>
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
          <RowMenu
            label={session.display_name ?? session.tmux_session}
            actions={[
              {
                label: 'Rename',
                // Just the label: tmux_session — and the home directory,
                // worktree and branch it derives — stays exactly as created.
                disabledReason: session.is_mine ? undefined : 'not yours',
                onSelect: () => onRenameSession?.(session),
              },
              {
                label: 'Configure',
                detail:
                  'MCP servers, skills, permissions and CLAUDE.md for this session only.',
                disabledReason:
                  session.is_mine || project.access === 'admin'
                    ? undefined
                    : "someone else's",
                onSelect: () => onConfigureSession?.(session),
              },
              {
                label: 'Close session',
                danger: true,
                detail:
                  'Ends the agent and removes its worktree. Its branch is kept.',
                disabledReason:
                  session.is_mine || project.access === 'admin'
                    ? undefined
                    : "someone else's",
                onSelect: () => onCloseSession?.(session),
              },
            ]}
          />
          </div>
        ))}
    </>
  )
}

export function ProjectView({
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
  // Optional: blank names itself after you, then -2, -3. Naming is worth
  // offering once there is more than one to tell apart.
  const [newSession, setNewSession] = useState('')
  // Blank means "wherever /workspace already is" — the behaviour before this
  // existed. Fetched lazily, and only while there is somewhere to show it:
  // it costs a round trip to the server, and nobody is picking a starting
  // branch while they are already inside a session.
  const [newBranch, setNewBranch] = useState('')
  const branches = useResource<string[]>(
    () =>
      session === null && project.status === 'running' && canControl(project.access)
        ? api.branches(project.id)
        : Promise.resolve([]),
    [project.id, project.status, project.access, session === null],
  )
  const active = sessions.find((s) => s.tmux_session === session) ?? null
  const mine = sessions.filter((s) => s.is_mine)
  // Set briefly when a keystroke is refused, so the explanation reacts to the
  // attempt instead of sitting there having already been read and dismissed.
  const [nudged, setNudged] = useState(false)
  // A terminal is unusable on a phone, and attaching one would also drag the
  // desktop's tmux window down to phone width. Default by screen size, but
  // leave it switchable: the feed is genuinely nicer for catching up, and the
  // terminal is still the only way to do anything unusual.
  const [view, setView] = useState<'terminal' | 'feed' | 'changes'>(() =>
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
          <button
            className={view === 'changes' ? 'active' : ''}
            onClick={() => setView('changes')}
            title="What this session has changed, committed or not"
          >
            Changes
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
      {project.status === 'running' && project.status_detail && (
        <div style={{ padding: '10px 16px 0' }}>
          <div className="banner info">{project.status_detail}</div>
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
                      <span className="session-name">{item.display_name ?? item.tmux_session}</span>
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
                    {item.state !== 'running' && canControl(project.access) && item.is_mine && (
                      <button
                        className="primary"
                        disabled={busy}
                        title="Reopen the conversation this session was having"
                        onClick={() =>
                          void act(async () => {
                            await api.startSession(project.id, false, item.tmux_session, true)
                            onEnter(item.tmux_session)
                          })
                        }
                      >
                        Resume
                      </button>
                    )}
                    <button
                      className="ghost"
                      title="Open in its own window — one per monitor, tiled by your window manager"
                      onClick={() =>
                        void act(() =>
                          openSessionWindow({
                            projectId: project.id,
                            session: item.tmux_session,
                            title: `${item.display_name ?? item.tmux_session} — ${project.name}`,
                            url: sessionWindowUrl(project.id, item.tmux_session),
                          }),
                        )
                      }
                    >
                      window
                    </button>
                    {(item.is_mine || project.access === 'admin') && (
                      // The other half of being able to start several: a list
                      // that only ever grows is its own kind of stuck. The
                      // branch survives this — it may hold the only copy of the
                      // work — so what goes is the agent and its worktree.
                      <button
                        className="ghost danger"
                        disabled={busy}
                        title="Close this session. Its branch is kept."
                        onClick={() =>
                          void act(() => api.deleteSession(project.id, item.tmux_session))
                        }
                      >
                        close
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
            {canControl(project.access) && (
              // More than one, and the button says so. A session is a whole
              // agent — its own home, its own worktree, its own branch — so
              // running two in a project is the ordinary way to have one
              // refactoring while another chases a bug. This used to disappear
              // once you had a session, which made one per project look like
              // the rule rather than the default.
              <div className="new-session">
                <input
                  value={newSession}
                  onChange={(e) => setNewSession(e.target.value)}
                  placeholder={mine.length ? 'Name it (optional)' : ''}
                  aria-label="New session name"
                  disabled={busy}
                  style={{ display: mine.length ? undefined : 'none' }}
                />
                <select
                  value={newBranch}
                  onChange={(e) => setNewBranch(e.target.value)}
                  aria-label="Starting branch"
                  title="Which branch the session's worktree starts from"
                  disabled={busy || branches.loading}
                >
                  <option value="">
                    {branches.data?.[0] ? `${branches.data[0]} (default)` : 'Default branch'}
                  </option>
                  {(branches.data ?? []).slice(1).map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const created = await api.createSession(
                        project.id,
                        newSession.trim() || undefined,
                        newBranch.trim() || undefined,
                      )
                      setNewSession('')
                      setNewBranch('')
                      onEnter(created.tmux_session)
                    })
                  }
                >
                  {mine.length ? 'New session' : 'Start my session'}
                </button>
              </div>
            )}
          </div>
          <YourApp
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
          ) : view === 'changes' ? (
            <div className="changes-pane">
              {drivable && <SavePoints projectId={project.id} session={session} />}
              <Changes projectId={project.id} session={session} />
            </div>
          ) : (
            <div className="feed-pane">
              <Summary projectId={project.id} session={session} />
              <Feed
                projectId={project.id}
                session={session}
                running
                readOnly={!drivable}
                onRefusedInput={nudge}
              />
            </div>
          )}
          <YourApp
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
