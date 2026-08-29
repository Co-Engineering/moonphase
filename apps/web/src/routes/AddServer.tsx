import { useState, type FormEvent } from 'react'
import { api, type ServerBootstrap, type SshAuthMode } from '../lib/api'
import { copyText } from '../lib/clipboard'

interface Props {
  onClose: () => void
  onCreated: () => void
}

const MODES: { value: SshAuthMode; label: string; blurb: string }[] = [
  {
    value: 'password_bootstrap',
    label: 'Password (once)',
    blurb:
      'Moonphase logs in with this password once, installs its own key, verifies it works, ' +
      'then discards the password. The only credential kept is one Moonphase generated.',
  },
  {
    value: 'managed_key',
    label: 'Moonphase-managed key',
    blurb:
      'Moonphase generates a keypair and shows you the public half to install yourself. ' +
      'Nothing of yours is ever sent to the backend.',
  },
  {
    value: 'provided_key',
    label: 'Paste my private key',
    blurb:
      'Fastest, but Moonphase then holds a key that probably opens more than this one ' +
      'machine, and revoking it means touching every server it works on.',
  },
]

export function AddServer({ onClose, onCreated }: Props) {
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [port, setPort] = useState(22)
  const [sshUser, setSshUser] = useState('root')
  const [mode, setMode] = useState<SshAuthMode>('password_bootstrap')
  const [password, setPassword] = useState('')
  const [privateKey, setPrivateKey] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [autoInstallDocker, setAutoInstallDocker] = useState(true)
  const [autoInstallSysbox, setAutoInstallSysbox] = useState(false)
  const [expectedFingerprint, setExpectedFingerprint] = useState('')

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ServerBootstrap | null>(null)

  /**
   * Watch the server come up.
   *
   * Bootstrapping installs a key, probes for Docker and often installs it,
   * which on a cold machine takes minutes. Waiting on one long request meant
   * the browser gave up on a bootstrap that went on to succeed and reported a
   * network error for a server that was fine.
   */
  const watch = async (id: string): Promise<ServerBootstrap> => {
    const deadline = Date.now() + 8 * 60 * 1000
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 2000))
      const server = await api.server(id)
      if (server.status !== 'bootstrapping' && server.status !== 'pending') {
        return {
          server,
          status: server.status,
          detail: server.status_detail ?? null,
          public_key_to_install: server.managed_public_key ?? null,
        }
      }
      if (Date.now() > deadline) {
        return {
          server,
          status: 'error',
          detail: 'It is still connecting after eight minutes. Something is wrong.',
          public_key_to_install: null,
        }
      }
    }
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.createServer({
        name,
        host,
        port,
        ssh_user: sshUser,
        auth_mode: mode,
        password: mode === 'password_bootstrap' ? password : undefined,
        private_key: mode === 'provided_key' ? privateKey : undefined,
        passphrase: mode === 'provided_key' && passphrase ? passphrase : undefined,
        auto_install_docker: autoInstallDocker,
        auto_install_sysbox: autoInstallSysbox,
        expected_host_key_fingerprint: expectedFingerprint.trim() || undefined,
      })
      setResult(created)
      onCreated()

      const settled =
        created.status === 'bootstrapping' || created.status === 'pending'
          ? await watch(created.server.id)
          : created
      setResult(settled)
      onCreated()

      if (settled.status === 'online') {
        onClose()
      } else if (settled.status === 'error') {
        // Nothing half-added left behind: a server that never connected is not
        // a server, and leaving it in the sidebar means tidying up after a
        // typo. The form keeps what was typed, so fixing it is one edit.
        await api.deleteServer(created.server.id).catch(() => {})
        setResult(null)
        onCreated()
        setError(settled.detail ?? 'Could not connect to that machine.')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const retry = async () => {
    if (!result) return
    setBusy(true)
    setError(null)
    try {
      const started = await api.bootstrapServer(result.server.id)
      const next =
        started.status === 'bootstrapping' || started.status === 'pending'
          ? await watch(result.server.id)
          : started
      setResult(next)
      onCreated()
      if (next.status === 'online') onClose()
      else if (next.status === 'error') setError(next.detail ?? 'Could not connect.')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // Once a key needs installing, the form is done — show instructions instead.
  if (result && result.status === 'awaiting_key_install' && result.public_key_to_install) {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="card modal" onClick={(e) => e.stopPropagation()}>
          <h2>Install the Moonphase key</h2>
          <p className="hint">
            Run this on <code>{result.server.host}</code> as{' '}
            <code>{result.server.ssh_user}</code>, then retry. The matching private key
            never leaves the backend.
          </p>
          <div className="keyblock">
            {`mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '${result.public_key_to_install}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`}
          </div>
          {error && <div className="banner error">{error}</div>}
          <div className="actions">
            <button
              className="primary"
              onClick={retry}
              disabled={busy}
            >
              {busy ? 'Checking…' : 'Retry connection'}
            </button>
            <button
              onClick={() =>
                void copyText(
                  `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '${result.public_key_to_install}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`,
                ).then((ok) => {
                  if (!ok) setError('Could not copy. Select the command and copy it.')
                })
              }
            >
              Copy command
            </button>
            <div className="spacer" />
            <button onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    )
  }

  const activeMode = MODES.find((m) => m.value === mode)!

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>Add server</h2>
        <p className="hint">
          Moonphase connects over SSH and runs one isolated Docker container per project.
        </p>

        <form onSubmit={submit}>
          {error && <div className="banner error">{error}</div>}
          {result?.status === 'error' && result.detail && (
            <div className="banner error">{result.detail}</div>
          )}

          <label>
            <span>Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="srv-hetzner"
              required
            />
          </label>

          <div className="row">
            <label style={{ flex: 3 }}>
              <span>Host</span>
              <input
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="49.12.0.1"
                required
              />
            </label>
            <label style={{ flex: 1 }}>
              <span>Port</span>
              <input
                type="number"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                min={1}
                max={65535}
              />
            </label>
          </div>

          <label>
            <span>SSH user</span>
            <input value={sshUser} onChange={(e) => setSshUser(e.target.value)} required />
          </label>

          <label>
            <span>Authentication</span>
            <select value={mode} onChange={(e) => setMode(e.target.value as SshAuthMode)}>
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            {activeMode.blurb}
          </p>

          {mode === 'password_bootstrap' && (
            <label>
              <span>Password (used once, then discarded)</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </label>
          )}

          {mode === 'provided_key' && (
            <>
              <label>
                <span>Private key</span>
                <textarea
                  value={privateKey}
                  onChange={(e) => setPrivateKey(e.target.value)}
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  required
                />
              </label>
              <label>
                <span>Passphrase (optional)</span>
                <input
                  type="password"
                  value={passphrase}
                  onChange={(e) => setPassphrase(e.target.value)}
                />
              </label>
            </>
          )}

          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="checkbox"
              checked={autoInstallDocker}
              onChange={(e) => setAutoInstallDocker(e.target.checked)}
              style={{ width: 'auto' }}
            />
            <span style={{ margin: 0 }}>Install Docker if missing (needs sudo)</span>
          </label>

          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="checkbox"
              checked={autoInstallSysbox}
              onChange={(e) => setAutoInstallSysbox(e.target.checked)}
              style={{ width: 'auto' }}
            />
            <span style={{ margin: 0 }}>Install Sysbox (lets projects run their own Docker)</span>
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            Only needed if a project on this server will run Docker inside its container.
            Requires an Ubuntu or Debian host.
          </p>

          <label>
            <span>Expected host key fingerprint (optional)</span>
            <input
              value={expectedFingerprint}
              onChange={(e) => setExpectedFingerprint(e.target.value)}
              placeholder="SHA256:..."
            />
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            Moonphase trusts whatever host key the server presents the first time it
            connects, then refuses to connect again if it ever changes. Fill this in — from{' '}
            <code>ssh-keyscan</code> or a provider&apos;s console — to have the first
            connection checked too, instead of trusted blindly. Required if this instance has
            trust-on-first-use disabled.
          </p>

          <div className="actions">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Connecting…' : 'Add server'}
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
