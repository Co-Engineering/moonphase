import { useState } from 'react'
import { api, type Server, type UserNamespaces } from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * Whether this server lets Antigravity's Auto Mode work.
 *
 * Auto Mode runs commands in agy's terminal sandbox, which is built on
 * unprivileged user namespaces, and Ubuntu restricts those to programs an
 * AppArmor profile allows. Only the host can change that — a container can at
 * most hide the setting from agy, which then believes in a sandbox the kernel
 * still refuses — so it is the server owner's call, made here. Nothing is
 * shown on a server whose kernel has no such restriction.
 */
export function UserNamespacesCard({ server }: { server: Server }) {
  const status = useResource<UserNamespaces>(() => api.userNamespaces(server.id), [server.id])
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const info = status.data
  if (!info || (info.restricted !== true && !info.lifted_by_moonphase)) return null

  const set = async (allow: boolean) => {
    setBusy(true)
    setError(null)
    try {
      await api.setUserNamespaces(server.id, allow)
      await status.reload(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div className="card">
      <h2>Antigravity Auto Mode</h2>
      {error && <div className="banner error">{error}</div>}
      {info.restricted ? (
        <>
          <p className="hint">
            Auto Mode runs commands in agy’s terminal sandbox, which needs user
            namespaces. This server’s kernel only allows them for programs an AppArmor
            profile names (<code>kernel.apparmor_restrict_unprivileged_userns = 1</code>,
            Ubuntu’s default), so agy says the sandbox is unavailable and asks about every
            command instead.
          </p>
          <p className="hint">
            Allowing them is a change to the whole server, not just one project: any
            program on it can then create a user namespace, as on Debian, Fedora and most
            other distributions. Moonphase writes it to{' '}
            <code>/etc/sysctl.d/60-moonphase-userns.conf</code> so it survives a reboot,
            and you can put the restriction back here. Needs passwordless sudo. Sessions
            pick it up when their harness next restarts.
          </p>
          <p className="hint">
            It reaches projects with Docker access turned on, which run under Sysbox.
            Docker’s own default seccomp filter blocks user namespaces in every other
            container, so agy’s sandbox stays unavailable there either way.
          </p>
          {confirming ? (
            <span className="row-menu-confirm">
              <button className="primary" disabled={busy} onClick={() => void set(true)}>
                Allow user namespaces
              </button>
              <button disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </span>
          ) : (
            <div className="actions">
              <button disabled={busy} onClick={() => setConfirming(true)}>
                Allow Auto Mode on this server…
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <p className="hint">
            User namespaces are allowed on this server, by Moonphase, so agy’s terminal
            sandbox can start and Auto Mode works. Putting the restriction back stops
            Auto Mode in sessions started after that.
          </p>
          <div className="actions">
            <button disabled={busy} onClick={() => void set(false)}>
              Restore the restriction
            </button>
          </div>
        </>
      )}
    </div>
  )
}
