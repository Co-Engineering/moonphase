import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type GitHubDevice, type HarnessLogin, type WorkspaceProfile } from '../lib/api'

interface Props {
  onClose: () => void
  onSaved: () => void
}

type Tab = 'accounts' | 'harness' | 'environment'

/**
 * Global settings.
 *
 * Everything here applies to every server and every project, now and in the
 * future. Nothing in this dialog is ever asked for again when adding a server
 * or creating a project — that was the whole complaint.
 */
export function Settings({ onClose, onSaved }: Props) {
  const [tab, setTab] = useState<Tab>('accounts')
  const [profile, setProfile] = useState<WorkspaceProfile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setProfile(await api.profile())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const run = async (fn: () => Promise<unknown>, message?: string) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await fn()
      await load()
      onSaved()
      if (message) setNotice(message)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <h2>Settings</h2>
        <p className="hint">
          Applied to every project on every server. Changes reach a running project when
          its harness next restarts.
        </p>

        <div className="tabs">
          {(['accounts', 'harness', 'environment'] as Tab[]).map((t) => (
            <button
              key={t}
              className={`tab${tab === t ? ' active' : ''}`}
              onClick={() => setTab(t)}
            >
              {t === 'accounts' ? 'Accounts' : t === 'harness' ? 'Claude' : 'Environment'}
            </button>
          ))}
        </div>

        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner info">{notice}</div>}

        {!profile ? (
          <p className="hint">Loading…</p>
        ) : tab === 'accounts' ? (
          <AccountsTab profile={profile} busy={busy} run={run} />
        ) : tab === 'harness' ? (
          <HarnessSettingsTab profile={profile} busy={busy} run={run} />
        ) : (
          <EnvironmentTab profile={profile} busy={busy} run={run} />
        )}

        <div className="actions" style={{ marginTop: 18 }}>
          <div className="spacer" />
          <button onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
}

type Runner = (fn: () => Promise<unknown>, message?: string) => Promise<void>

// --- accounts ---------------------------------------------------------------

function AccountsTab({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  return (
    <>
      <ClaudeAccount profile={profile} busy={busy} run={run} />
      <GitHubAccount profile={profile} busy={busy} run={run} />
    </>
  )
}

function ClaudeAccount({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  const [login, setLogin] = useState<HarnessLogin | null>(null)
  const [code, setCode] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const pollRef = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearInterval(pollRef.current), [])

  const start = async () => {
    setWorking(true)
    setError(null)
    try {
      setLogin(await api.startHarnessLogin())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setWorking(false)
    }
  }

  /**
   * Submitting only types the code; the exchange happens on the harness's own
   * schedule and each poll advances it one step. Waiting for the whole thing
   * inside one request is what made this look like it had hung.
   */
  const submit = async () => {
    if (!login) return
    setWorking(true)
    setError(null)
    try {
      const started = await api.submitHarnessCode(login.session_id, code)
      setLogin(started)
      window.clearInterval(pollRef.current)
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api.pollHarnessLogin(started.session_id)
          setLogin(next)
          if (next.state === 'complete' || next.state === 'error') {
            window.clearInterval(pollRef.current)
            setWorking(false)
            if (next.state === 'complete') {
              setCode('')
              setLogin(null)
              await run(async () => {}, 'Signed in to Claude. Every project will use it.')
            } else {
              setError(next.detail ?? 'Sign-in failed.')
            }
          }
        } catch (err) {
          window.clearInterval(pollRef.current)
          setWorking(false)
          setError(err instanceof Error ? err.message : String(err))
        }
      }, 2000)
    } catch (err) {
      setWorking(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const cancel = () => {
    window.clearInterval(pollRef.current)
    setLogin(null)
    setWorking(false)
    setCode('')
  }

  if (profile.harness_connected) {
    return (
      <div className="card inner">
        <div className="row-between">
          <div>
            <h3>
              <span className="dot connected" /> Claude
            </h3>
            <p className="hint">
              Connected via{' '}
              {profile.harness_auth_mode === 'oauth'
                ? 'your Claude subscription'
                : 'an API key'}
              . Every project uses this — no per-project sign-in.
            </p>
          </div>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void run(() => api.disconnectHarness(), 'Claude disconnected.')}
          >
            Disconnect
          </button>
        </div>
      </div>
    )
  }

  const verifying = login?.state === 'verifying'

  return (
    <div className="card inner">
      <h3>
        <span className="dot" /> Claude
      </h3>
      <p className="hint">
        Sign in once here. Moonphase stores the credential encrypted and injects it into
        every project container.
      </p>

      {error && <div className="banner error">{error}</div>}

      {/* The terminal is shown whenever something is happening or went wrong,
          so a flow that stalls is diagnosable rather than a spinner. */}
      {(verifying || error) && login?.pane && (
        <details className="pane-details" open={Boolean(error)}>
          <summary>Terminal output</summary>
          <pre className="pane">{login.pane}</pre>
        </details>
      )}

      {login?.state === 'awaiting_code' || verifying ? (
        <>
          <p className="hint">
            <strong>1.</strong> Open this URL and approve:
          </p>
          <div className="keyblock">
            <a href={login?.url ?? ''} target="_blank" rel="noreferrer">
              {login?.url}
            </a>
          </div>
          <div className="actions" style={{ marginBottom: 12 }}>
            <button onClick={() => void navigator.clipboard.writeText(login?.url ?? '')}>
              Copy URL
            </button>
          </div>
          <p className="hint">
            <strong>2.</strong> Paste the code it gives you:
          </p>
          <label>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Authorization code"
              disabled={verifying}
              autoFocus
            />
          </label>
          <div className="actions">
            <button
              className="primary"
              disabled={working || verifying || !code.trim()}
              onClick={submit}
            >
              {verifying ? 'Waiting for Claude…' : 'Finish sign-in'}
            </button>
            <button onClick={cancel}>Cancel</button>
          </div>
        </>
      ) : (
        <>
          <div className="actions">
            <button className="primary" disabled={working} onClick={start}>
              {working ? 'Starting…' : 'Sign in with Claude'}
            </button>
            <button onClick={() => setShowKey((v) => !v)}>
              {showKey ? 'Hide API key option' : 'Use an API key instead'}
            </button>
          </div>

          {showKey && (
            <div style={{ marginTop: 14 }}>
              <label>
                <span>Anthropic API key</span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-ant-…"
                />
              </label>
              <button
                className="primary"
                disabled={busy || !apiKey.trim()}
                onClick={() =>
                  void run(() => api.setHarnessApiKey(apiKey.trim()), 'API key saved.')
                }
              >
                Save key
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function GitHubAccount({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  const [deviceFlow, setDeviceFlow] = useState<boolean | null>(null)
  const [device, setDevice] = useState<GitHubDevice | null>(null)
  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const pollRef = useRef<number | undefined>(undefined)

  useEffect(() => {
    void api
      .githubAvailable()
      .then((r) => setDeviceFlow(r.device_flow))
      .catch(() => setDeviceFlow(false))
    return () => window.clearInterval(pollRef.current)
  }, [])

  const start = async () => {
    setWorking(true)
    setError(null)
    try {
      const session = await api.startGitHubDevice()
      setDevice(session)
      // GitHub rate-limits this endpoint, so respect the interval it asks for.
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api.pollGitHubDevice(session.session_id)
          setDevice(next)
          if (next.state === 'complete' || next.state === 'error') {
            window.clearInterval(pollRef.current)
            if (next.state === 'complete') {
              setDevice(null)
              await run(async () => {}, `GitHub connected as @${next.account}.`)
            } else {
              setError(next.detail ?? 'GitHub sign-in failed.')
            }
          }
        } catch (err) {
          window.clearInterval(pollRef.current)
          setError(err instanceof Error ? err.message : String(err))
        }
      }, Math.max(session.interval, 5) * 1000)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setWorking(false)
    }
  }

  if (profile.github_connected) {
    return (
      <div className="card inner">
        <div className="row-between">
          <div>
            <h3>
              <span className="dot connected" /> GitHub
            </h3>
            <p className="hint">
              Connected as <code>@{profile.github_account}</code>. Private repositories
              clone and push in every project, and <code>gh</code> works in the terminal.
            </p>
          </div>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void run(() => api.disconnectGitHub(), 'GitHub disconnected.')}
          >
            Disconnect
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="card inner">
      <h3>
        <span className="dot" /> GitHub
      </h3>
      <p className="hint">
        Lets projects clone and push private repositories without pasting credentials per
        project.
      </p>

      {error && <div className="banner error">{error}</div>}

      {device?.state === 'awaiting_authorization' ? (
        <>
          <p className="hint">
            Open{' '}
            <a href={device.verification_uri ?? ''} target="_blank" rel="noreferrer">
              {device.verification_uri}
            </a>{' '}
            and enter this code:
          </p>
          <div className="devicecode">{device.user_code}</div>
          <p className="hint">Waiting for you to approve on GitHub…</p>
        </>
      ) : (
        <div className="actions">
          {deviceFlow && (
            <button className="primary" disabled={working} onClick={start}>
              {working ? 'Starting…' : 'Connect GitHub'}
            </button>
          )}
          <button onClick={() => setShowToken((v) => !v)}>
            {showToken ? 'Hide token option' : 'Use a personal access token'}
          </button>
        </div>
      )}

      {deviceFlow === false && !showToken && (
        <p className="hint" style={{ marginTop: 10 }}>
          No GitHub OAuth app is configured on this deployment, so use a personal access
          token. Set <code>MOONPHASE_GITHUB_CLIENT_ID</code> to enable one-click sign-in.
        </p>
      )}

      {showToken && (
        <div style={{ marginTop: 14 }}>
          <label>
            <span>Personal access token (needs `repo` scope)</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="ghp_… or github_pat_…"
            />
          </label>
          <button
            className="primary"
            disabled={busy || !token.trim()}
            onClick={() => void run(() => api.setGitHubToken(token.trim()), 'GitHub connected.')}
          >
            Save token
          </button>
        </div>
      )}
    </div>
  )
}

// --- harness settings -------------------------------------------------------

function HarnessSettingsTab({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  const [settings, setSettings] = useState(profile.claude_settings_json ?? '')
  const [claudeMd, setClaudeMd] = useState(profile.claude_md ?? '')
  const [mcp, setMcp] = useState(profile.mcp_json ?? '')

  const save = () =>
    run(
      () =>
        api.saveProfile({
          claude_settings_json: settings.trim() || null,
          claude_md: claudeMd.trim() || null,
          mcp_json: mcp.trim() || null,
          env_vars: profile.env_vars,
          git_user_name: profile.git_user_name,
          git_user_email: profile.git_user_email,
        }),
      'Saved. Restart a harness to pick it up.',
    )

  return (
    <>
      <label>
        <span>
          Global CLAUDE.md — written to <code>~/.claude/CLAUDE.md</code> in every project
        </span>
        <textarea
          value={claudeMd}
          onChange={(e) => setClaudeMd(e.target.value)}
          placeholder={'# My preferences\n\n- Prefer small, focused commits\n- Never add comments that restate the code'}
          rows={8}
        />
      </label>

      <label>
        <span>
          settings.json — written to <code>~/.claude/settings.json</code>
        </span>
        <textarea
          value={settings}
          onChange={(e) => setSettings(e.target.value)}
          placeholder={'{\n  "permissions": {\n    "allow": ["Bash(npm run test)"]\n  }\n}'}
          rows={8}
        />
      </label>

      <label>
        <span>
          MCP servers — written to <code>~/.claude/.mcp.json</code>
        </span>
        <textarea
          value={mcp}
          onChange={(e) => setMcp(e.target.value)}
          placeholder={'{\n  "mcpServers": {}\n}'}
          rows={6}
        />
      </label>

      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => void save()}>
          Save settings
        </button>
      </div>
    </>
  )
}

// --- environment ------------------------------------------------------------

function EnvironmentTab({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  const [gitName, setGitName] = useState(profile.git_user_name ?? '')
  const [gitEmail, setGitEmail] = useState(profile.git_user_email ?? '')
  const [pairs, setPairs] = useState<[string, string][]>(
    Object.entries(profile.env_vars ?? {}),
  )

  const save = () => {
    const env: Record<string, string> = {}
    for (const [key, value] of pairs) {
      if (key.trim()) env[key.trim()] = value
    }
    return run(
      () =>
        api.saveProfile({
          claude_settings_json: profile.claude_settings_json,
          claude_md: profile.claude_md,
          mcp_json: profile.mcp_json,
          env_vars: env,
          git_user_name: gitName.trim() || null,
          git_user_email: gitEmail.trim() || null,
        }),
      'Saved. Restart a harness to pick it up.',
    )
  }

  return (
    <>
      <h3>Git identity</h3>
      <p className="hint">
        So commits the agent makes are attributed to you rather than to <code>dev</code>.
      </p>
      <div className="row">
        <label>
          <span>Name</span>
          <input value={gitName} onChange={(e) => setGitName(e.target.value)} placeholder="Ada Lovelace" />
        </label>
        <label>
          <span>Email</span>
          <input
            value={gitEmail}
            onChange={(e) => setGitEmail(e.target.value)}
            placeholder="ada@example.com"
          />
        </label>
      </div>

      <h3 style={{ marginTop: 18 }}>Environment variables</h3>
      <p className="hint">
        Available to the harness and anything it runs, in every project. Stored encrypted.
      </p>

      {pairs.map(([key, value], index) => (
        <div className="row" key={index} style={{ marginBottom: 8 }}>
          <input
            value={key}
            placeholder="NAME"
            onChange={(e) => {
              const next = [...pairs]
              next[index] = [e.target.value, value]
              setPairs(next)
            }}
          />
          <input
            value={value}
            placeholder="value"
            onChange={(e) => {
              const next = [...pairs]
              next[index] = [key, e.target.value]
              setPairs(next)
            }}
          />
          <button
            className="ghost"
            style={{ flex: '0 0 auto' }}
            onClick={() => setPairs(pairs.filter((_, i) => i !== index))}
          >
            ✕
          </button>
        </div>
      ))}

      <div className="actions">
        <button onClick={() => setPairs([...pairs, ['', '']])}>Add variable</button>
        <div className="spacer" />
        <button className="primary" disabled={busy} onClick={() => void save()}>
          Save
        </button>
      </div>
    </>
  )
}
