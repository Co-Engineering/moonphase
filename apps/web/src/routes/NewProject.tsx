import { useState, type FormEvent } from 'react'
import { api, type HarnessInfo, type Server } from '../lib/api'

interface Props {
  servers: Server[]
  harnesses: HarnessInfo[]
  defaultServerId?: string
  onClose: () => void
  onCreated: (projectId: string) => void
}

export function NewProject({
  servers,
  harnesses,
  defaultServerId,
  onClose,
  onCreated,
}: Props) {
  const online = servers.filter((s) => s.status === 'online')
  const [serverId, setServerId] = useState(defaultServerId ?? online[0]?.id ?? '')
  const [name, setName] = useState('')
  const [harness, setHarness] = useState(harnesses[0]?.kind ?? 'claude_code')
  const [repoUrl, setRepoUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [previewPort, setPreviewPort] = useState('')
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
        harness_auth_mode: apiKey.trim() ? 'api_key' : null,
        api_key: apiKey.trim() || null,
        preview_port: previewPort ? Number(previewPort) : null,
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
          Creates an isolated container on the server with its own workspace volume, then
          starts the harness in a tmux session that outlives every client.
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
              placeholder="https://github.com/you/repo.git"
            />
          </label>

          <label>
            <span>Anthropic API key (optional)</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-ant-…"
            />
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            Leave blank to sign in with your Claude subscription from inside the terminal on
            first attach. Stored encrypted either way.
          </p>

          <label>
            <span>Preview port (optional)</span>
            <input
              type="number"
              value={previewPort}
              onChange={(e) => setPreviewPort(e.target.value)}
              placeholder="3000"
              min={1}
              max={65535}
            />
          </label>

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
