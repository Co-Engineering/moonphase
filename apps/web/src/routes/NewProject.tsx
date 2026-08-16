import { useState, type FormEvent } from 'react'
import { api, type HarnessInfo, type Server } from '../lib/api'

interface Props {
  servers: Server[]
  harnesses: HarnessInfo[]
  defaultServerId?: string
  connected: boolean
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
  defaultServerId,
  connected,
  onClose,
  onCreated,
  onOpenSettings,
}: Props) {
  const online = servers.filter((s) => s.status === 'online')
  const [serverId, setServerId] = useState(defaultServerId ?? online[0]?.id ?? '')
  const [name, setName] = useState('')
  const [harness, setHarness] = useState(harnesses[0]?.kind ?? 'claude_code')
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

          {!connected && (
            <div className="banner warn">
              Claude is not connected yet, so the harness will start signed out.{' '}
              <button type="button" className="linkish" onClick={onOpenSettings}>
                Sign in once in Settings
              </button>{' '}
              and every project picks it up.
            </div>
          )}

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

          <label>
            <span>Harness</span>
            <select value={harness} onChange={(e) => setHarness(e.target.value)}>
              {harnesses.map((h) => (
                <option key={h.kind} value={h.kind} disabled={!h.available}>
                  {h.display_name}
                </option>
              ))}
            </select>
          </label>

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
