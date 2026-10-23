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
  type AccountTarget,
  type GitHubDevice,
  type HarnessAccount,
  type HarnessInfo,
  type HarnessKind,
  type HarnessLogin,
  type McpOAuthConnectionInfo,
  type PushStatus,
  type WorkspaceProfile,
} from '../lib/api'
import { copyText } from '../lib/clipboard'
import { useResource } from '../lib/useResource'
import { accountName } from '../components/AccountPicker'
import { harnessCopy } from '../lib/harness'
import { playAlertSound, soundChoiceLabel, SOUND_CHOICES, type SoundChoice } from '../lib/sound'
import { useSoundAlertPreference } from '../lib/soundAlertPreference'

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
              ['harness', 'Agent settings'],
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

/**
 * Which harnesses get an account card, beyond Claude's.
 *
 * Only Claude Code could be connected from here once, so no other harness
 * ever became `configured`, and New project only offers configured ones —
 * a harness could be installed, registered and listed, and still impossible
 * to pick. Each one here has a sign-in and a key the card knows how to word
 * (see `harnessCopy`). OpenCode and Pydantic AI are not here yet: they take
 * a provider's API key, and which provider is a question this card does not
 * ask.
 */
const OTHER_ACCOUNT_CARDS: HarnessKind[] = ['antigravity']

function AccountsTab({
  profile,
  busy,
  run,
}: {
  profile: WorkspaceProfile
  busy: boolean
  run: Runner
}) {
  // What each harness supports, and whether this instance runs it at all. A
  // card for a harness the backend does not list would sign in to something
  // no project can use.
  const harnesses = useResource<HarnessInfo[]>(() => api.harnesses(), [])
  const info = (kind: HarnessKind) => harnesses.data?.find((h) => h.kind === kind)
  return (
    <>
      {/* Always shown, listed or not: it is the one every instance has, and
          an older backend's answer — or none yet — must not hide it. */}
      <HarnessAccountCard
        harness="claude_code"
        info={info('claude_code')}
        harnesses={harnesses.data}
        busy={busy}
        run={run}
      />
      {OTHER_ACCOUNT_CARDS.map((kind) => {
        const listed = info(kind)
        return listed ? (
          <HarnessAccountCard
            key={kind}
            harness={kind}
            info={listed}
            harnesses={harnesses.data}
            busy={busy}
            run={run}
          />
        ) : null
      })}
      <GitHubAccount profile={profile} busy={busy} run={run} />
      <NotificationsPanel run={run} />
    </>
  )
}

/**
 * The sentences on a harness's account card that are about which projects
 * use it. Claude's are what they always were: every project ran on it.
 * Anything else only runs the projects created for it.
 */
function cardCopy(harness: HarnessKind, agent: string) {
  if (harness === 'claude_code') {
    return {
      intro:
        'Sign in once here. Moonphase stores the credential encrypted and injects it into ' +
        'every project container. You can add more accounts afterwards — a work one, a ' +
        'client’s — and choose per project or per session.',
      caveat: null,
      signedIn: 'Signed in to Claude. Every project will use it.',
      onlyOne:
        'Every project uses this account. Add another to run some projects or sessions on a different subscription.',
    }
  }
  return {
    intro:
      `Sign in once here and ${agent} becomes a choice in New project. Moonphase stores ` +
      `the credential encrypted and injects it into the container of every project that ` +
      `runs ${agent}. You can add more accounts afterwards and choose per project or per ` +
      'session.',
    // Google's FAQ, not an inference: it says wrapping agy in third-party
    // apps is not supported on consumer plans, and recommends an API key for
    // that. Moonphase runs agy's own interactive terminal on your sign-in and
    // never switches accounts by itself, but the person deciding which to
    // use should hear it from here rather than find out later.
    caveat:
      harness === 'antigravity'
        ? 'Google’s Antigravity FAQ says its consumer plans do not support using agy ' +
          'through third-party apps, and recommends a Gemini API key for that.'
        : null,
    signedIn: `Signed in to ${agent}. New project now offers it.`,
    onlyOne:
      `Every ${agent} project uses this account. Add another to run some projects or ` +
      'sessions on a different one.',
  }
}

function HarnessAccountCard({
  harness,
  info,
  harnesses,
  busy,
  run,
}: {
  harness: HarnessKind
  /** From `/api/harnesses`. Undefined until it answers, or on a backend too old to. */
  info?: HarnessInfo
  harnesses?: HarnessInfo[] | null
  busy: boolean
  run: Runner
}) {
  const copy = harnessCopy(harness, harnesses)
  const words = cardCopy(harness, copy.agent)
  const name = (account: HarnessAccount) => accountName(account, harness)
  const accounts = useResource<HarnessAccount[]>(() => api.harnessAccounts(harness), [harness])
  const list = accounts.data ?? []
  // What the sign-in panel below is for: a new account, or one already in the
  // list being signed in again.
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [renewing, setRenewing] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameTo, setRenameTo] = useState('')
  const [removing, setRemoving] = useState<string | null>(null)

  const change = async (fn: () => Promise<unknown>, message: string) => {
    await run(async () => {
      await fn()
      await accounts.reload(true)
    }, message)
  }

  const signedIn = async (message: string) => {
    setAdding(false)
    setRenewing(null)
    setNewName('')
    await accounts.reload(true)
    await run(async () => {}, message)
  }

  if (accounts.loading && !accounts.data) {
    return (
      <div className="card inner">
        <h3>
          <span className="dot" /> {copy.agent}
        </h3>
        <p className="hint">Loading…</p>
      </div>
    )
  }

  if (list.length === 0) {
    return (
      <div className="card inner">
        <h3>
          <span className="dot" /> {copy.agent}
        </h3>
        <p className="hint">{words.intro}</p>
        {words.caveat && <p className="hint">{words.caveat}</p>}
        <HarnessSignIn
          harness={harness}
          info={info}
          harnesses={harnesses}
          target={{ label: 'Personal' }}
          onDone={() => void signedIn(words.signedIn)}
        />
      </div>
    )
  }

  return (
    <div className="card inner">
      <h3>
        <span className="dot connected" /> {copy.agent}
      </h3>
      <p className="hint">
        {list.length === 1
          ? words.onlyOne
          : 'New sessions run on the default unless their project or the session itself picks another.'}
      </p>
      <ul className="account-list">
        {list.map((account) => (
          <li key={account.id}>
            <div className="account-list-main">
              {renaming === account.id ? (
                <span className="row-menu-confirm">
                  <input
                    value={renameTo}
                    onChange={(e) => setRenameTo(e.target.value)}
                    aria-label="Account name"
                    autoFocus
                    maxLength={80}
                  />
                  <button
                    className="primary"
                    disabled={busy || !renameTo.trim()}
                    onClick={() =>
                      void change(
                        () => api.updateHarnessAccount(account.id, { label: renameTo.trim() }),
                        'Renamed.',
                      ).then(() => setRenaming(null))
                    }
                  >
                    Save
                  </button>
                  <button onClick={() => setRenaming(null)}>Cancel</button>
                </span>
              ) : (
                <>
                  <strong>{name(account)}</strong>
                  {account.is_default && <span className="shared-tag">default</span>}
                  <span className="hint">
                    {account.auth_mode === 'oauth' ? copy.oauthAccount : copy.keyAccount}
                  </span>
                </>
              )}
            </div>
            {renaming !== account.id && (
              <div className="account-list-actions">
                {removing === account.id ? (
                  <span className="row-menu-confirm">
                    <span className="hint">
                      Remove it? Anything using it moves to
                      {account.is_default ? ' another account' : ' the default'}.
                    </span>
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={() =>
                        void change(
                          () => api.deleteHarnessAccount(account.id),
                          `${name(account)} removed.`,
                        ).then(() => setRemoving(null))
                      }
                    >
                      Remove
                    </button>
                    <button onClick={() => setRemoving(null)}>Cancel</button>
                  </span>
                ) : (
                  <>
                    {!account.is_default && (
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() =>
                          void change(
                            () => api.updateHarnessAccount(account.id, { is_default: true }),
                            `${name(account)} is now the default.`,
                          )
                        }
                      >
                        Make default
                      </button>
                    )}
                    <button
                      className="link"
                      onClick={() => {
                        setRenaming(account.id)
                        setRenameTo(account.label ?? '')
                      }}
                    >
                      Rename
                    </button>
                    <button
                      className="link"
                      onClick={() => {
                        setAdding(false)
                        setRenewing(renewing === account.id ? null : account.id)
                      }}
                    >
                      Sign in again
                    </button>
                    <button className="link danger" onClick={() => setRemoving(account.id)}>
                      Remove
                    </button>
                  </>
                )}
              </div>
            )}
            {renewing === account.id && (
              <HarnessSignIn
                harness={harness}
                info={info}
                harnesses={harnesses}
                target={{ accountId: account.id }}
                onDone={() => void signedIn(`${name(account)} signed in again.`)}
                onCancel={() => setRenewing(null)}
              />
            )}
          </li>
        ))}
      </ul>

      {adding ? (
        <div className="account-add">
          <label>
            <span>Name it</span>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Work, a client's name…"
              maxLength={80}
              autoFocus
            />
          </label>
          {newName.trim() ? (
            <HarnessSignIn
              harness={harness}
              info={info}
              harnesses={harnesses}
              target={{ label: newName.trim() }}
              onDone={() => void signedIn(`${newName.trim()} added.`)}
              onCancel={() => setAdding(false)}
            />
          ) : (
            <div className="actions">
              <button onClick={() => setAdding(false)}>Cancel</button>
            </div>
          )}
        </div>
      ) : (
        <div className="actions">
          <button
            onClick={() => {
              setRenewing(null)
              setAdding(true)
            }}
          >
            Add another account
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * One sign-in to `harness`, by its own sign-in or by API key, landing in
 * `target`: an existing account to replace, or a new account by that name.
 *
 * Which of the two is offered is the harness's to say. `/api/harnesses`
 * reports both — `login_supported`, `supported_auth_modes` — and this used to
 * fetch them and read neither, offering Claude's subscription sign-in and an
 * Anthropic key field for anything. Until that answer arrives (or from a
 * backend too old to give it) both are offered, which is what Claude always
 * had.
 */
function HarnessSignIn({
  harness,
  info,
  harnesses,
  target,
  onDone,
  onCancel,
}: {
  harness: HarnessKind
  info?: HarnessInfo
  harnesses?: HarnessInfo[] | null
  target: AccountTarget
  onDone: () => void
  onCancel?: () => void
}) {
  const copy = harnessCopy(harness, harnesses)
  const canSignIn = info ? info.login_supported : true
  const canUseKey = info ? info.supported_auth_modes.includes('api_key') : true
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
      const started = await api.startHarnessLogin(target, harness)
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
              onDone()
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
    onCancel?.()
  }

  const saveKey = async () => {
    setWorking(true)
    setError(null)
    try {
      await api.setHarnessApiKey(apiKey.trim(), target, harness)
      setApiKey('')
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setWorking(false)
    }
  }

  const verifying = login?.state === 'verifying'

  const keyField = (
    <>
      <label>
        <span>{copy.keyLabel}</span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={copy.keyPlaceholder}
        />
      </label>
      <button
        className="primary"
        disabled={working || !apiKey.trim()}
        onClick={() => void saveKey()}
      >
        Save key
      </button>
    </>
  )

  return (
    <div className="claude-signin">
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
            <strong>1.</strong> {copy.openStep}
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
            <strong>2.</strong> {copy.codeStep}
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
              {verifying ? `Waiting for ${copy.agent}…` : 'Finish sign-in'}
            </button>
            <button onClick={cancel}>Cancel</button>
          </div>
        </>
      ) : !canSignIn ? (
        // Nothing to sign in to, so the key is the whole of it rather than an
        // alternative hidden behind a button.
        <>
          {canUseKey ? (
            keyField
          ) : (
            <p className="hint">{copy.name} cannot be connected from here.</p>
          )}
          {onCancel && (
            <div className="actions">
              <button onClick={cancel}>Cancel</button>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="actions">
            <button className="primary" disabled={working} onClick={start}>
              {working ? 'Starting…' : copy.signIn}
            </button>
            {login?.state === 'starting' ? (
              // Waiting on a container build is a long time to be stuck with no
              // way out but closing the dialog.
              <button onClick={cancel}>Cancel</button>
            ) : (
              <>
                {canUseKey && (
                  <button onClick={() => setShowKey((v) => !v)}>
                    {showKey ? 'Hide API key option' : copy.keyOption}
                  </button>
                )}
                {onCancel && <button onClick={cancel}>Cancel</button>}
              </>
            )}
          </div>

          {showKey && canUseKey && <div style={{ marginTop: 14 }}>{keyField}</div>}
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
  const [gitName, setGitName] = useState(profile.git_user_name ?? '')
  const [gitEmail, setGitEmail] = useState(profile.git_user_email ?? '')

  // Picks up a value set elsewhere — most notably the server prefilling it
  // from the GitHub account this component just connected.
  useEffect(() => {
    setGitName(profile.git_user_name ?? '')
    setGitEmail(profile.git_user_email ?? '')
  }, [profile.git_user_name, profile.git_user_email])

  useEffect(() => {
    void api
      .githubAvailable()
      .then((r) => setDeviceFlow(r.device_flow))
      .catch(() => setDeviceFlow(false))
    return () => window.clearInterval(pollRef.current)
  }, [])

  const saveIdentity = () =>
    run(
      () =>
        api.saveProfile({
          claude_settings_json: profile.claude_settings_json,
          claude_md: profile.claude_md,
          mcp_json: profile.mcp_json,
          skills: profile.skills,
          env_vars: profile.env_vars,
          git_user_name: gitName.trim() || null,
          git_user_email: gitEmail.trim() || null,
        }),
      'Saved.',
    )

  const identitySection = (
    <div style={{ marginTop: 18 }}>
      <h3>Git identity</h3>
      <p className="hint">
        So commits the agent makes are attributed to you rather than to <code>dev</code>.
        Prefilled from GitHub once connected, if it knows a name or email — either way,
        yours to change.
      </p>
      <div className="row">
        <label>
          <span>Name</span>
          <input
            value={gitName}
            onChange={(e) => setGitName(e.target.value)}
            placeholder="Ada Lovelace"
          />
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
      <div className="actions">
        <div className="spacer" />
        <button className="primary" disabled={busy} onClick={() => void saveIdentity()}>
          Save
        </button>
      </div>
    </div>
  )

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
        {identitySection}
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

      {identitySection}
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
    // Not editable here — Settings → Workspace already owns this at the org
    // scope — but ClaudeConfigValue still needs a value to carry.
    env_vars: profile.env_vars,
  })
  const [connecting, setConnecting] = useState<string | null>(null)
  const [connectError, setConnectError] = useState<string | null>(null)
  const [connections, setConnections] = useState<McpOAuthConnectionInfo[]>([])

  const loadConnections = () =>
    void api.mcpOAuthConnections().then(setConnections).catch(() => setConnections([]))
  useEffect(loadConnections, [])

  const currentProfile = () => ({
    claude_settings_json: config.claude_settings_json,
    claude_md: config.claude_md?.trim() || null,
    mcp_json: config.mcp_json,
    skills: config.skills,
    env_vars: profile.env_vars,
    git_user_name: profile.git_user_name,
    git_user_email: profile.git_user_email,
  })

  const save = () =>
    run(() => api.saveProfile(currentProfile()), 'Saved. Restart a harness to pick it up.')

  // The relay looks the server up in the *saved* profile — `claude mcp
  // login` runs against whatever was last written to the session's own
  // ~/.claude.json, not whatever is still sitting in this tab's own state.
  // Connecting before saving used to relay against a server the container
  // did not know about yet, with a timeout that blamed the wrong thing.
  const onConnectMcp = async (name: string) => {
    setConnectError(null)
    try {
      await api.saveProfile(currentProfile())
      setConnecting(name)
    } catch (err) {
      setConnectError(err instanceof Error ? err.message : String(err))
    }
  }

  // Same "save what's about to be checked" reasoning as onConnectMcp — and
  // same choice to leave errors uncaught: McpEditor's own runCheck already
  // turns a rejection into a banner of its own.
  const onCheckMcp = async () => {
    await api.saveProfile(currentProfile())
    return api.checkMcpHealthForOrg()
  }

  return (
    <>
      {connectError && <div className="banner error">{connectError}</div>}
      <ClaudeConfigFields
        value={config}
        onChange={setConfig}
        claudeMdHint="Written to ~/.claude/CLAUDE.md for Claude Code and ~/.gemini/AGENTS.md for Antigravity, so it applies to every project"
        instructionTabLabel="Instructions"
        onConnectMcp={(name) => void onConnectMcp(name)}
        onCheckMcp={onCheckMcp}
        mcpConnections={connections}
        showEnvVars={false}
      />

      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => void save()}>
          Save settings
        </button>
      </div>

      <ConnectedMcpServers connections={connections} reload={loadConnections} />

      {connecting && (
        // No project or session in hand here at all — relays through any one
        // of the caller's own running sessions anywhere, the backend's pick.
        <McpConnectDialog
          target={{ scope: 'org' }}
          serverName={connecting}
          onClose={() => setConnecting(null)}
          onConnected={() => {
            setConnecting(null)
            loadConnections()
          }}
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
function ConnectedMcpServers({
  connections,
  reload,
}: {
  connections: McpOAuthConnectionInfo[]
  reload: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const disconnect = async (name: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.disconnectMcpOAuth(name)
      reload()
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
      {connections.length === 0 ? (
        <p className="muted small">
          None yet — use Connect next to an HTTP or SSE server above to add one.
        </p>
      ) : (
        connections.map((c) => (
          <div className="row-between" key={c.id}>
            <span>{c.server_name}</span>
            <button className="danger" disabled={busy} onClick={() => void disconnect(c.server_name)}>
              Disconnect
            </button>
          </div>
        ))
      )}
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
          // Set from Accounts → GitHub, not here — echoed back unchanged so
          // saving environment variables never clobbers it.
          git_user_name: profile.git_user_name,
          git_user_email: profile.git_user_email,
        }),
      'Saved. Restart a harness to pick it up.',
    )
  }

  return (
    <>
      <h3>Environment variables</h3>
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
    const editingCustom = items.some((e) => e.key === draft.key && !e.builtin)
    const customisingBuiltin = !editingCustom && items.some((e) => e.key === draft.key && e.builtin)
    return (
      <>
        <h3>{editingCustom ? 'Edit' : customisingBuiltin ? 'Customise' : 'New'} environment</h3>
        <p className="hint">
          Any Debian or Ubuntu family image works — Moonphase installs tmux, the harness
          and its own tooling on top. The image is built on your server, so nothing needs
          publishing anywhere.
        </p>
        {customisingBuiltin && (
          <p className="hint">
            Saved under the same key as the built-in it came from, so every project already
            using it switches to your version automatically — nothing to update per project.
          </p>
        )}

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
  const [soundPreference, updateSoundPreference] = useSoundAlertPreference()

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
      if (result.delivered > 0) {
        setNotice(`Sent to ${result.delivered} device${result.delivered === 1 ? '' : 's'}.`)
      } else if (result.subscriptions === 0) {
        setNotice('No devices are subscribed yet.')
      } else {
        // Subscribed, but the push service itself refused or could not be
        // reached — the previous version of this screen said "Sent" here
        // regardless, which is worse than saying nothing.
        setError(result.errors[0] ?? 'Could not deliver to any subscribed device.')
      }
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
        Tells you when an agent finishes or gets stuck on a question, so you can close the
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

      <div style={{ marginTop: 18 }}>
        <h3>Sound alert</h3>
        <p className="hint">
          Push is for when you have walked away. This is for the opposite case — sitting
          right here with a different session open when one of yours starts waiting on you.
        </p>
        <label className="check">
          <input
            type="checkbox"
            checked={soundPreference.enabled}
            onChange={(e) => updateSoundPreference({ enabled: e.target.checked })}
          />
          <span>Play a sound when a session needs me</span>
        </label>
        {soundPreference.enabled && (
          <div className="actions">
            <select
              value={soundPreference.choice}
              aria-label="Sound"
              onChange={(e) => updateSoundPreference({ choice: e.target.value as SoundChoice })}
            >
              {SOUND_CHOICES.map((choice) => (
                <option key={choice} value={choice}>
                  {soundChoiceLabel(choice)}
                </option>
              ))}
            </select>
            <button type="button" onClick={() => playAlertSound(soundPreference.choice)}>
              Preview
            </button>
          </div>
        )}
      </div>

      {/* `run` is unused here: enabling push changes nothing the rest of the
          settings dialog displays. */}
      {void run}
    </div>
  )
}
