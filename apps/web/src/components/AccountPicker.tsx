import { useState } from 'react'
import { api, type HarnessAccount, type Project, type Session } from '../lib/api'

export function accountName(account: HarnessAccount): string {
  return account.label || (account.auth_mode === 'oauth' ? 'Claude subscription' : 'API key')
}

function byId(accounts: HarnessAccount[], id: string | null): HarnessAccount | undefined {
  return id ? accounts.find((a) => a.id === id) : undefined
}

/** What a session with no account of its own would run on in this project. */
export function projectAccount(project: Project, accounts: HarnessAccount[]) {
  return byId(accounts, project.harness_credential_id) ?? accounts.find((a) => a.is_default)
}

/**
 * Which of your accounts a project's sessions run on. Only shown once there
 * is more than one to choose between, and only to whoever owns the project —
 * it is their subscription being spent.
 */
export function ProjectAccountSelect({
  project,
  accounts,
  onChanged,
}: {
  project: Project
  accounts: HarnessAccount[]
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fallback = accounts.find((a) => a.is_default)

  const choose = async (value: string) => {
    setBusy(true)
    setError(null)
    try {
      await api.setProjectAccount(project.id, value || null)
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-row">
      <label className="account-select">
        <span>Your sessions here run on</span>
        <select
          value={project.harness_credential_id ?? ''}
          disabled={busy}
          onChange={(e) => void choose(e.target.value)}
        >
          <option value="">
            {fallback ? `Your default (${accountName(fallback)})` : 'Your default'}
          </option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {accountName(a)}
            </option>
          ))}
        </select>
      </label>
      <span className="hint">New and restarted sessions use it; a session can pick its own.</span>
      {error && <div className="banner error">{error}</div>}
    </div>
  )
}

/**
 * Move one of your sessions to another account. Claude reads its login only
 * when it starts, so this restarts it — and asks first, because the agent
 * stops mid-thought, even though the conversation resumes.
 */
export function SessionAccountSwitcher({
  project,
  session,
  accounts,
  onSwitched,
}: {
  project: Project
  session: Session
  accounts: HarnessAccount[]
  onSwitched: () => void
}) {
  const [pending, setPending] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const projectDefault = projectAccount(project, accounts)

  const confirm = async () => {
    if (pending === null) return
    setBusy(true)
    setError(null)
    try {
      await api.switchSessionAccount(project.id, session.tmux_session, pending || null)
      setPending(null)
      onSwitched()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const target = pending === null ? undefined : (byId(accounts, pending) ?? projectDefault)

  return (
    <span className="account-switcher">
      <select
        aria-label="Claude account"
        title="Which of your Claude accounts this session runs on"
        value={pending ?? session.harness_credential_id ?? ''}
        disabled={busy}
        onChange={(e) =>
          setPending(e.target.value === (session.harness_credential_id ?? '') ? null : e.target.value)
        }
      >
        <option value="">
          {projectDefault ? `${accountName(projectDefault)} (project's)` : "Project's account"}
        </option>
        {accounts.map((a) => (
          <option key={a.id} value={a.id}>
            {accountName(a)}
          </option>
        ))}
      </select>
      {pending !== null && (
        <span className="row-menu-confirm">
          <span className="hint">
            Restart on {target ? accountName(target) : 'that account'}? The conversation
            resumes.
          </span>
          <button className="primary" disabled={busy} onClick={() => void confirm()}>
            {busy ? 'Switching…' : 'Switch'}
          </button>
          <button disabled={busy} onClick={() => setPending(null)}>
            Cancel
          </button>
        </span>
      )}
      {error && <span className="banner error">{error}</span>}
    </span>
  )
}

/**
 * Choosing an account from a menu. The session list and the top bar are not
 * always on screen — a session opened from the sidebar skips the list, and
 * the top bar hides its buttons on a narrow window — so the sidebar's own
 * menus open this instead.
 */
export function AccountDialog({
  project,
  session,
  accounts,
  onChanged,
  onClose,
}: {
  project: Project
  /** Present to move one session; absent to set the project's account. */
  session?: Session
  accounts: HarnessAccount[]
  onChanged: () => void
  onClose: () => void
}) {
  const title = session
    ? `Claude account — ${session.display_name ?? session.tmux_session}`
    : `Claude account — ${project.name}`
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="row-between">
          <h2>{title}</h2>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
        {accounts.length < 2 ? (
          <p className="hint">
            You have one Claude account, so everything runs on it. Add another under
            Settings → Accounts to choose between them here.
          </p>
        ) : session ? (
          <>
            <p className="hint">
              Switching restarts Claude in this session on the other account and resumes
              the same conversation.
            </p>
            <SessionAccountSwitcher
              project={project}
              session={session}
              accounts={accounts}
              onSwitched={() => {
                onChanged()
                onClose()
              }}
            />
          </>
        ) : (
          <ProjectAccountSelect project={project} accounts={accounts} onChanged={onChanged} />
        )}
      </div>
    </div>
  )
}
