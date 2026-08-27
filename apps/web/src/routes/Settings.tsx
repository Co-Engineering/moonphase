import { useCallback, useEffect, useRef, useState } from 'react'
import {
  disable as disablePush,
  enable as enablePush,
  isApplePhone,
  isInstalled,
  pushSupport,
} from '../lib/notifications'
import { InstallPrompt } from '../components/InstallPrompt'
import { ClaudeConfigFields, type ClaudeConfigValue } from '../components/ClaudeConfig'
import { McpConnectDialog } from '../components/McpConnectDialog'
import { InstanceTab } from '../components/InstanceTab'
import { PeopleTab } from '../components/PeopleTab'
import {
  api,
  instance,
  type Environment,
  type GitHubDevice,
  type HarnessLogin,
  type McpOAuthConnectionInfo,
  type PushStatus,
  type WorkspaceProfile,
} from '../lib/api'
import { copyText } from '../lib/clipboard'

interface Props {
  onClose: () => void
  onSaved: () => void
}

type Tab = 'accounts' | 'harness' | 'environments' | 'workspace' | 'instance' | 'people'

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
  // Whether to offer the Instance tab at all. The endpoints refuse either way;
  // this decides whether anyone is shown a door they cannot open.
  const [isAdmin, setIsAdmin] = useState(false)

  useEffect(() => {
    void instance
      .me()
      .then((me) => setIsAdmin(me.is_instance_admin))
      .catch(() => {
        // An instance too old to answer has no such screen to offer.
      })
  }, [])

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
          {(
            [
              ['accounts', 'Accounts'],
              ['harness', 'Claude'],
              ['environments', 'Environments'],
              ['workspace', 'Workspace'],
              // Only for whoever administers the instance. Everyone else has no
              // business seeing the list of accounts, let alone the buttons.
              ...(isAdmin
                ? ([
                    ['instance', 'Instance'],
                    ['people', 'People'],
                  ] as [Tab, string][])
                : []),
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              className={`tab${tab === key ? ' active' : ''}`}
              onClick={() => setTab(key)}
            >
              {label}
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
        ) : tab === 'environments' ? (
          <EnvironmentsTab busy={busy} run={run} />
        ) : tab === 'instance' ? (
          <InstanceTab busy={busy} run={run} />
        ) : tab === 'people' ? (
          <PeopleTab busy={busy} run={run} />
        ) : (
          <WorkspaceTab profile={profile} busy={busy} run={run} />
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
      <NotificationsPanel run={run} />
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
  const [copied, setCopied] = useState<'copied' | 'failed' | null>(null)
  const pollRef = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearInterval(pollRef.current), [])

  /**
   * Starting only asks for a sign-in; the URL arrives later.
   *
   * Preparing one means building an environment image on a server that may
   * never have run a container, starting it, and waiting for the harness to
   * print a URL — minutes on a cold machine. Waiting for all of that inside the
   * request had the browser give up and report a network error for a sign-in
   * that was working, so the state to watch for is `starting` and the session
   * says what it is doing while it lasts.
   */
  const start = async () => {
    setWorking(true)
    setError(null)
    try {
      const started = await api.startHarnessLogin()
      setLogin(started)
      if (started.state !== 'starting') {
        setWorking(false)
        return
      }
      window.clearInterval(pollRef.current)
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api.pollHarnessLogin(started.session_id)
          setLogin(next)
          if (next.state === 'starting') return
          window.clearInterval(pollRef.current)
          setWorking(false)
          if (next.state === 'error') {
            setError(next.detail ?? 'Could not start the sign-in.')
            setLogin(null)
          }
        } catch (err) {
          window.clearInterval(pollRef.current)
          setWorking(false)
          setError(err instanceof Error ? err.message : String(err))
        }
      }, 2000)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
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

      {/* The slow step is a container build, and a button that says "Starting…"
          for four minutes is indistinguishable from one that is stuck. */}
      {login?.state === 'starting' && (
        <div className="banner">{login.detail ?? 'Preparing the sign-in…'}</div>
      )}

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
            <button
              onClick={() =>
                void copyText(login?.url ?? '').then((ok) =>
                  // Said out loud either way. A copy button that silently does
                  // nothing is worse than no button, and that is exactly what
                  // this was on an instance without HTTPS.
                  setCopied(ok ? 'copied' : 'failed'),
                )
              }
            >
              {copied === 'copied' ? 'Copied' : 'Copy URL'}
            </button>
            {copied === 'failed' && (
              <span className="hint">
                Could not copy — select the link above and copy it.
              </span>
            )}
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
            {login?.state === 'starting' ? (
              // Waiting on a container build is a long time to be stuck with no
              // way out but closing the dialog.
              <button onClick={cancel}>Cancel</button>
            ) : (
              <button onClick={() => setShowKey((v) => !v)}>
                {showKey ? 'Hide API key option' : 'Use an API key instead'}
              </button>
            )}
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
  const [config, setConfig] = useState<ClaudeConfigValue>({
    claude_settings_json: profile.claude_settings_json,
    claude_md: profile.claude_md,
    mcp_json: profile.mcp_json,
    skills: profile.skills,
  })
  const [connecting, setConnecting] = useState<string | null>(null)

  const save = () =>
    run(
      () =>
        api.saveProfile({
          claude_settings_json: config.claude_settings_json,
          claude_md: config.claude_md?.trim() || null,
          mcp_json: config.mcp_json,
          skills: config.skills,
          env_vars: profile.env_vars,
          git_user_name: profile.git_user_name,
          git_user_email: profile.git_user_email,
        }),
      'Saved. Restart a harness to pick it up.',
    )

  return (
    <>
      <ClaudeConfigFields
        value={config}
        onChange={setConfig}
        claudeMdHint="Written to ~/.claude/CLAUDE.md, so it applies to every project"
        onConnectMcp={(name) => setConnecting(name)}
      />

      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => void save()}>
          Save settings
        </button>
      </div>

      <ConnectedMcpServers />

      {connecting && (
        // No project or session in hand here at all — relays through any one
        // of the caller's own running sessions anywhere, the backend's pick.
        <McpConnectDialog
          target={{ scope: 'org' }}
          serverName={connecting}
          onClose={() => setConnecting(null)}
          onConnected={() => setConnecting(null)}
        />
      )}
    </>
  )
}

/**
 * MCP servers connected via OAuth, org-wide.
 *
 * Connecting one happens from a session's own Configure dialog — it needs a
 * running container to relay through — but disconnecting does not, so it
 * lives here instead of leaving no way to revoke one without starting a
 * session just to do it.
 */
function ConnectedMcpServers() {
  const [connections, setConnections] = useState<McpOAuthConnectionInfo[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = () => void api.mcpOAuthConnections().then(setConnections).catch(() => setConnections([]))
  useEffect(load, [])

  if (!connections || connections.length === 0) return null

  const disconnect = async (name: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.disconnectMcpOAuth(name)
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card inner" style={{ marginTop: 16 }}>
      <h3>Connected MCP servers</h3>
      <p className="hint">Connected via OAuth, available to every session in this org.</p>
      {error && <div className="banner error">{error}</div>}
      {connections.map((c) => (
        <div className="row-between" key={c.id}>
          <span>{c.server_name}</span>
          <button className="danger" disabled={busy} onClick={() => void disconnect(c.server_name)}>
            Disconnect
          </button>
        </div>
      ))}
    </div>
  )
}

// --- environment ------------------------------------------------------------

function WorkspaceTab({
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
          skills: profile.skills,
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

// --- environments -----------------------------------------------------------

const BLANK: EnvironmentInput = {
  key: '',
  display_name: '',
  description: '',
  base_image: '',
  setup_script: '',
}

type EnvironmentInput = {
  key: string
  display_name: string
  description: string
  base_image: string
  setup_script: string
}

/**
 * Defining an environment.
 *
 * An environment is a base image plus optional setup commands; Moonphase
 * layers tmux, the harness and the rest on top and builds it on the server the
 * first time a project uses it. There is no registry to push to and nothing to
 * pre-build, so adding one is data entry.
 */
function EnvironmentsTab({ busy, run }: { busy: boolean; run: Runner }) {
  const [items, setItems] = useState<Environment[]>([])
  const [draft, setDraft] = useState<EnvironmentInput | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      setItems(await api.environments())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    if (!draft) return
    await run(async () => {
      await api.saveEnvironment({
        key: draft.key,
        display_name: draft.display_name,
        description: draft.description || null,
        base_image: draft.base_image,
        setup_script: draft.setup_script || null,
      })
      await load()
      setDraft(null)
    }, 'Environment saved. It builds on the server the first time a project uses it.')
  }

  const remove = (env: Environment) =>
    run(async () => {
      await api.deleteEnvironment(env.key)
      await load()
    }, `Removed ${env.display_name}.`)

  if (draft) {
    return (
      <>
        <h3>{items.some((e) => e.key === draft.key && !e.builtin) ? 'Edit' : 'New'} environment</h3>
        <p className="hint">
          Any Debian or Ubuntu family image works — Moonphase installs tmux, the harness
          and its own tooling on top. The image is built on your server, so nothing needs
          publishing anywhere.
        </p>

        {error && <div className="banner error">{error}</div>}

        <div className="row">
          <label>
            <span>Name</span>
            <input
              value={draft.display_name}
              onChange={(e) => {
                const display_name = e.target.value
                setDraft({
                  ...draft,
                  display_name,
                  // Derive the key while it is untouched; it is immutable once saved.
                  key:
                    draft.key === '' || draft.key === slugify(draft.display_name)
                      ? slugify(display_name)
                      : draft.key,
                })
              }}
              placeholder="Rust nightly"
              autoFocus
            />
          </label>
          <label>
            <span>Key</span>
            <input
              value={draft.key}
              onChange={(e) => setDraft({ ...draft, key: e.target.value })}
              placeholder="rust-nightly"
            />
          </label>
        </div>

        <label>
          <span>Base image</span>
          <input
            value={draft.base_image}
            onChange={(e) => setDraft({ ...draft, base_image: e.target.value })}
            placeholder="debian:bookworm-slim"
          />
        </label>
        <p className="hint" style={{ marginTop: -6 }}>
          For example <code>ubuntu:22.04</code>, <code>python:3.12-bookworm</code>,{' '}
          <code>node:20-bookworm</code> or <code>nvidia/cuda:12.4.1-devel-ubuntu22.04</code>.
        </p>

        <label>
          <span>Description (optional)</span>
          <input
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="What this is for"
          />
        </label>

        <label>
          <span>Setup commands (optional)</span>
          <textarea
            value={draft.setup_script}
            onChange={(e) => setDraft({ ...draft, setup_script: e.target.value })}
            placeholder={
              'apt-get update\napt-get install -y --no-install-recommends postgresql-client\nrm -rf /var/lib/apt/lists/*'
            }
            rows={6}
          />
        </label>
        <p className="hint" style={{ marginTop: -6 }}>
          Run as root during the build, exactly as written. Anything a Dockerfile{' '}
          <code>RUN</code> could do.
        </p>

        <div className="actions">
          <button
            className="primary"
            disabled={busy || !draft.key.trim() || !draft.base_image.trim()}
            onClick={() => void save()}
          >
            Save environment
          </button>
          <div className="spacer" />
          <button onClick={() => setDraft(null)}>Cancel</button>
        </div>
      </>
    )
  }

  return (
    <>
      <p className="hint">
        What a project&apos;s container is built from. Moonphase adds tmux, git, the
        harness and its tunnelling tools to whichever base you choose, then builds the
        image on the server the first time it is needed.
      </p>

      {error && <div className="banner error">{error}</div>}
      {loading && <p className="hint">Loading…</p>}

      {items.map((env) => (
        <div className="card inner" key={env.key}>
          <div className="row-between">
            <div style={{ minWidth: 0 }}>
              <h3>
                {env.display_name}
                {env.builtin && <span className="badge">built-in</span>}
              </h3>
              <p className="hint">
                <code>{env.base_image}</code>
                {env.description ? ` — ${env.description}` : ''}
              </p>
              {env.setup_script && (
                <details className="pane-details">
                  <summary>Setup commands</summary>
                  <pre className="pane">{env.setup_script}</pre>
                </details>
              )}
              {env.project_count > 0 && (
                <p className="hint" style={{ marginBottom: 0 }}>
                  Used by {env.project_count} project{env.project_count === 1 ? '' : 's'}.
                </p>
              )}
            </div>
            <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
              <button
                onClick={() =>
                  setDraft({
                    // Editing a built-in creates a custom one with the same key,
                    // which shadows it everywhere without touching any project.
                    key: env.key,
                    display_name: env.display_name,
                    description: env.description ?? '',
                    base_image: env.base_image,
                    setup_script: env.setup_script ?? '',
                  })
                }
              >
                {env.builtin ? 'Customise' : 'Edit'}
              </button>
              {!env.builtin && (
                <button className="danger" disabled={busy} onClick={() => void remove(env)}>
                  Delete
                </button>
              )}
            </div>
          </div>
        </div>
      ))}

      <div className="actions">
        <button className="primary" onClick={() => setDraft({ ...BLANK })}>
          New environment
        </button>
      </div>
    </>
  )
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
}

// --- notifications ----------------------------------------------------------

/**
 * Enabling push.
 *
 * This is what makes leaving actually work: without it you have to keep
 * opening the app to find out whether Claude got stuck, which is the waiting
 * Moonphase exists to remove.
 */
function NotificationsPanel({ run }: { run: Runner }) {
  const [status, setStatus] = useState<PushStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [working, setWorking] = useState(false)

  const support = pushSupport()

  const load = useCallback(async () => {
    try {
      setStatus(await api.pushStatus())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = async () => {
    if (!status) return
    setWorking(true)
    setError(null)
    setNotice(null)
    try {
      if (status.subscribed) {
        await disablePush()
      } else {
        if (!status.public_key) throw new Error('This deployment has no VAPID key.')
        await enablePush(status.public_key)
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setWorking(false)
    }
  }

  const test = async () => {
    setWorking(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.testPush()
      setNotice(
        result.delivered > 0
          ? `Sent to ${result.delivered} device${result.delivered === 1 ? '' : 's'}.`
          : 'No devices are subscribed yet.',
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="card inner">
      <h3>
        <span className={`dot${status?.subscribed ? ' connected' : ''}`} /> Notifications
      </h3>
      <p className="hint">
        Tells you when Claude finishes or gets stuck on a question, so you can close the
        app and walk away instead of checking it. They arrive through your phone&rsquo;s
        own notification system, so they show up with the app closed and the screen off.
      </p>

      <InstallPrompt />

      {support.supported && !isInstalled() && !isApplePhone() && (
        <p className="hint">
          Installing Moonphase to your home screen is worth doing anyway: notifications
          then look and behave like any other app&rsquo;s, including the count on the icon.
        </p>
      )}

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner info">{notice}</div>}

      {!support.supported ? (
        <div className="banner warn">
          <strong>{support.reason}</strong>
          {support.fix && (
            <>
              <br />
              {support.fix}
            </>
          )}
        </div>
      ) : status && !status.configured ? (
        <div className="banner warn">
          This deployment has no VAPID keypair, so it cannot send push. Run{' '}
          <code>python scripts/gen_vapid.py &gt;&gt; .env</code> and restart the API.
        </div>
      ) : (
        <div className="actions">
          <button
            className={status?.subscribed ? '' : 'primary'}
            disabled={working || !status}
            onClick={() => void toggle()}
          >
            {working
              ? 'Working…'
              : status?.subscribed
                ? 'Turn off on this device'
                : 'Enable on this device'}
          </button>
          {status?.subscribed && (
            <button disabled={working} onClick={() => void test()}>
              Send a test
            </button>
          )}
        </div>
      )}
      {/* `run` is unused here: enabling push changes nothing the rest of the
          settings dialog displays. */}
      {void run}
    </div>
  )
}
