import { api, type AccountUsage, type LiveWindow } from '../lib/api'
import { useResource } from '../lib/useResource'
import { resetLabel, untilLabel } from './Usage'

/**
 * Every Claude account's limits at a glance — what `/usage` shows inside
 * Claude Code, for all of them at once, without opening a session on each.
 *
 * Read from Anthropic with each account's own sign-in, a minute at a time.
 */
export function Dashboard({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const usage = useResource(() => api.accountUsage(), [], { pollMs: 60000 })
  const accounts = usage.data ?? []

  return (
    <>
      <div className="topbar">
        <button
          className="ghost"
          onClick={onToggleSidebar}
          title="Toggle sidebar"
          aria-label="Toggle sidebar"
        >
          ☰
        </button>
        <h1>Dashboard</h1>
        <span className="sub">Plan limits for every connected account</span>
        <div className="spacer" />
        <button disabled={usage.loading} onClick={() => void usage.reload(true)}>
          {usage.loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      <div className="content">
        {usage.error && <div className="banner error">{usage.error}</div>}
        {!usage.data ? (
          <p className="hint">Asking Anthropic…</p>
        ) : accounts.length === 0 ? (
          <div className="empty">
            <h3>No Claude accounts yet</h3>
            Sign in under Settings → Accounts to see its limits here.
          </div>
        ) : (
          <div className="dashboard-grid">
            {accounts.map((account) => (
              <AccountCard key={account.account_id} account={account} />
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function AccountCard({ account }: { account: AccountUsage }) {
  const name =
    account.label || (account.auth_mode === 'oauth' ? 'Claude subscription' : 'API key')
  return (
    <div className="card dashboard-card">
      <div className="row-between">
        <h2>
          {name} {account.is_default && <span className="shared-tag">default</span>}
        </h2>
        <span className="hint">
          {account.auth_mode === 'oauth' ? 'Claude subscription' : 'API key'}
        </span>
      </div>

      {account.windows.length > 0 && (
        <div className={`windows${account.stale ? ' stale' : ''}`}>
          {account.windows.map((w) => (
            <LiveWindowBar key={w.key} window={w} />
          ))}
        </div>
      )}

      {account.status !== 'ok' && (
        <p className={account.status === 'api_key' ? 'hint' : 'hint dashboard-problem'}>
          {account.stale && 'Out of date — '}
          {account.detail}
          {account.retry_at &&
            ` Trying again at ${new Date(account.retry_at).toLocaleTimeString(undefined, {
              hour: 'numeric',
              minute: '2-digit',
            })}.`}
        </p>
      )}
      {account.status === 'ok' && account.windows.length === 0 && (
        <p className="hint">Anthropic reported no limits for this account.</p>
      )}
      {account.fetched_at && (
        <p className="hint dashboard-updated">
          Updated {new Date(account.fetched_at).toLocaleTimeString()}
          {account.source === 'statusline' && ' · as your running session last saw it'}
          {account.source === 'session' && ' · read through one of your running sessions'}
        </p>
      )}
    </div>
  )
}

function LiveWindowBar({ window: w }: { window: LiveWindow }) {
  const percent = Math.round(w.percent)
  const tone = percent >= 90 ? 'hot' : percent >= 75 ? 'warm' : ''
  return (
    <div className="window">
      <div className="window-head">
        <span className="window-label">{w.label}</span>
        <span className="window-used">
          <strong>{percent}% used</strong>
        </span>
      </div>
      <div className={`bar ${tone}`}>
        <span style={{ width: `${Math.max(1, percent)}%` }} />
      </div>
      {w.resets_at && (
        <div className="window-foot">
          <span>{resetLabel(w.resets_at)}</span>
          {new Date(w.resets_at).getTime() > Date.now() && (
            <span className="muted">in {untilLabel(w.resets_at)}</span>
          )}
        </div>
      )}
    </div>
  )
}
