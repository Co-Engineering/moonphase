import { useCallback, useEffect, useState } from 'react'
import type { Session as AuthSession } from '@supabase/supabase-js'
import { supabase } from './lib/supabase'
import { api, type Project, type Server } from './lib/api'
import { useResource } from './lib/useResource'
import { ProjectTerminal } from './components/Terminal'
import { Auth } from './routes/Auth'
import { AddServer } from './routes/AddServer'
import { NewProject } from './routes/NewProject'

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

function Shell({ email }: { email: string }) {
  const [selected, setSelected] = useState<
    { kind: 'server' | 'project'; id: string } | null
  >(null)
  const [showAddServer, setShowAddServer] = useState(false)
  const [showNewProject, setShowNewProject] = useState(false)
  const [showSidebar, setShowSidebar] = useState(false)

  // Servers are polled: bootstrap and Docker installs finish out of band, and
  // a stale "bootstrapping" chip is the most confusing thing the UI can show.
  const servers = useResource<Server[]>(() => api.servers(), [], { pollMs: 15000 })
  const projects = useResource<Project[]>(() => api.projects(), [], { pollMs: 15000 })
  const harnesses = useResource(() => api.harnesses(), [])

  const reloadAll = useCallback(() => {
    void servers.reload(true)
    void projects.reload(true)
  }, [servers, projects])

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
          {servers.data?.length === 0 && (
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
              </button>

              {projects.data
                ?.filter((p) => p.server_id === server.id)
                .map((project) => (
                  <button
                    key={project.id}
                    className={`tree-row tree-project status-${project.status}${
                      activeProject?.id === project.id ? ' active' : ''
                    }`}
                    onClick={() => selectProject(project.id)}
                  >
                    <span className="dot" />
                    <span className="name">{project.name}</span>
                  </button>
                ))}
            </div>
          ))}

          <div className="section-label" style={{ marginTop: 12 }}>
            <button
              className="ghost"
              onClick={() => setShowNewProject(true)}
              disabled={!servers.data?.some((s) => s.status === 'online')}
            >
              + New project
            </button>
          </div>
        </div>

        <div className="sidebar-foot">
          <span title={email}>{email}</span>
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
          />
        ) : activeServer ? (
          <ServerView
            server={activeServer}
            onChanged={reloadAll}
            onNewProject={() => setShowNewProject(true)}
            onToggleSidebar={() => setShowSidebar(true)}
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

      {showAddServer && (
        <AddServer onClose={() => setShowAddServer(false)} onCreated={reloadAll} />
      )}
      {showNewProject && (
        <NewProject
          servers={servers.data ?? []}
          harnesses={harnesses.data ?? []}
          defaultServerId={activeServer?.id ?? activeProject?.server_id}
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

function ProjectView({
  project,
  onChanged,
  onToggleSidebar,
}: {
  project: Project
  onChanged: () => void
  onToggleSidebar: () => void
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

  return (
    <>
      <div className="topbar">
        <button className="ghost" onClick={onToggleSidebar}>
          ☰
        </button>
        <h1>{project.name}</h1>
        <span className="sub">
          {project.server_name} · {project.container_name}
        </span>
        <div className="spacer" />
        <button
          disabled={busy}
          onClick={() => void act(() => api.startSession(project.id, true))}
          title="Kill the tmux session and start the harness fresh"
        >
          Restart harness
        </button>
        {project.status === 'running' ? (
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
        )}
      </div>

      {error && (
        <div style={{ padding: '10px 16px 0' }}>
          <div className="banner error">{error}</div>
        </div>
      )}

      {project.status === 'running' ? (
        <div className="content flush">
          <ProjectTerminal projectId={project.id} />
        </div>
      ) : (
        <div className="content">
          <div className="empty">
            <h3>Container is {project.status}</h3>
            {project.status_detail ?? 'Start it to attach a terminal.'}
          </div>
        </div>
      )}
    </>
  )
}

function ServerView({
  server,
  onChanged,
  onNewProject,
  onToggleSidebar,
}: {
  server: Server
  onChanged: () => void
  onNewProject: () => void
  onToggleSidebar: () => void
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
        <div className="spacer" />
        <button disabled={busy} onClick={() => void act(() => api.testServer(server.id))}>
          Test
        </button>
        <button
          disabled={busy || server.status !== 'online'}
          onClick={() => void act(() => api.bootstrapServer(server.id))}
        >
          Re-bootstrap
        </button>
        <button className="primary" disabled={server.status !== 'online'} onClick={onNewProject}>
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

        {server.managed_public_key && (
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

        <div className="card">
          <h2>Danger zone</h2>
          <p className="hint">
            Removing the server deletes its projects from Moonphase and revokes the key
            above. Container volumes on the machine are left alone.
          </p>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void act(() => api.deleteServer(server.id))}
          >
            Remove server
          </button>
        </div>
      </div>
    </>
  )
}
