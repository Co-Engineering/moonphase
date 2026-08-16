import { useState, type FormEvent } from 'react'
import { api, type Environment, type HarnessInfo, type Server } from '../lib/api'

interface Props {
  servers: Server[]
  harnesses: HarnessInfo[]
  environments: Environment[]
  defaultServerId?: string
  onClose: () => void
  onCreated: (projectId: string) => void
  onOpenSettings: () => void
}

/**
 * Creating a project asks four questions and no credentials.
 *
 * Authentication and configuration come from the global profile, so this form
 * never grows a "paste your API key" field, and ports are discovered rather
 * than declared.
 */
export function NewProject({
  servers,
  harnesses,
  environments,
  defaultServerId,
  onClose,
  onCreated,
  onOpenSettings,
}: Props) {
  const online = servers.filter((s) => s.status === 'online')
  // Only harnesses that are both implemented and signed into. Offering one you
  // have not connected produces a project whose terminal comes up unable to do
  // anything, with nothing on screen explaining why.
  const usable = harnesses.filter((h) => h.available && h.configured)

  const [serverId, setServerId] = useState(defaultServerId ?? online[0]?.id ?? '')
  const [name, setName] = useState('')
  const [harness, setHarness] = useState(usable[0]?.kind ?? '')
  const [environment, setEnvironment] = useState(environments[0]?.key ?? 'debian')
  const [repoUrl, setRepoUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const project = await api.createProject({
        server_id: serverId,
        name,
        harness: harness as 'claude_code' | 'opencode',
        environment,
        repo_url: repoUrl.trim() || null,
      })
      // The API returns the row even when provisioning failed, so the user can
      // read the reason instead of losing what they typed.
      if (project.status === 'error') {
        setError(project.status_detail ?? 'Provisioning failed.')
        setBusy(false)
        return
      }
      onCreated(project.id)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  if (online.length === 0) {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="card modal" onClick={(e) => e.stopPropagation()}>
          <h2>No servers ready</h2>
          <p className="hint">
            Add a server and get it to <strong>online</strong> before creating a project.
          </p>
          <div className="actions">
            <button onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    )
  }

  if (usable.length === 0) {
    const loginable = harnesses.filter((h) => h.available)
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="card modal" onClick={(e) => e.stopPropagation()}>
          <h2>Connect a harness first</h2>
          <p className="hint">
            A project runs a coding agent, and none are connected yet. Sign in once
            and every project from then on uses it — you will not be asked again.
          </p>
          {loginable.length > 0 && (
            <p className="hint">
              Available: {loginable.map((h) => h.display_name).join(', ')}.
            </p>
          )}
          <div className="actions">
            <button className="primary" onClick={onOpenSettings}>
              Open Settings
            </button>
            <div className="spacer" />
            <button onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>New project</h2>
        <p className="hint">
          Creates an isolated container with its own workspace volume, then starts the
          harness in a tmux session that outlives every client.
        </p>

        <form onSubmit={submit}>
          {error && <div className="banner error">{error}</div>}

          <label>
            <span>Server</span>
            <select value={serverId} onChange={(e) => setServerId(e.target.value)} required>
              {online.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} — {s.ssh_user}@{s.host}
                </option>
              ))}
            </select>
          </label>

          <label>
            <span>Project name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="moonphase-api"
              required
            />
          </label>

          <div className="row">
            <label>
              <span>Harness</span>
              <select value={harness} onChange={(e) => setHarness(e.target.value)}>
                {usable.map((h) => (
                  <option key={h.kind} value={h.kind}>
                    {h.display_name}
                  </option>
                ))}
              </select>
            </label>

            <label>
              <span>Environment</span>
              <select
                value={environment}
                onChange={(e) => setEnvironment(e.target.value)}
              >
                {environments.map((env) => (
                  <option key={env.key} value={env.key}>
                    {env.display_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="hint" style={{ marginTop: -6 }}>
            {environments.find((e) => e.key === environment)?.description}
          </p>

          <label>
            <span>Repository (optional)</span>
            <input
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/you/private-repo.git"
            />
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            Private repositories work once GitHub is connected in Settings.
          </p>

          <div className="actions">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Provisioning…' : 'Create project'}
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
