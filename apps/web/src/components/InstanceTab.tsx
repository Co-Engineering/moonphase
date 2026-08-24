import { useCallback, useEffect, useState } from 'react'
import { instance, type InstanceSettings } from '../lib/api'
import * as api from '../lib/api'
import { SignInMethods, draftFrom, type Draft } from './SignInMethods'
import { UpdatePanel } from './UpdatePanel'

/**
 * The instance itself: its address, who may sign in, and who has an account.
 *
 * All of this existed already — in the setup screen, which runs exactly once
 * and is then unreachable. So the domain could not be corrected, sign-in could
 * not be reconfigured, and the only way to add a colleague was to reopen
 * registration to the entire internet and hope they got there first.
 *
 * Shown only to administrators of the instance, which is deliberately not the
 * same thing as owning an organization: every account owns one of those.
 */
export function InstanceTab({
  busy,
  run,
}: {
  busy: boolean
  run: (fn: () => Promise<unknown>, message?: string) => Promise<void>
}) {
  const [settings, setSettings] = useState<InstanceSettings | null>(null)
  const [methods, setMethods] = useState<Draft>(() => draftFrom(null))
  const [redirectUri, setRedirectUri] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [found, auth] = await Promise.all([
        instance.settings(),
        api.authMethods(),
      ])
      setSettings(found)
      setMethods(draftFrom(auth))
      setRedirectUri(auth.redirect_uri)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (error) return <div className="banner error">{error}</div>
  if (!settings) return <p className="hint">Loading…</p>

  // Google and Microsoft refuse to redirect to a bare IP, so offering them
  // before a domain exists offers a button that cannot work.
  const domainMissing = !settings.public_url

  return (
    <div className="tab-body">
      <div className="card inner">
        <h3>Address</h3>
        <p className="hint">
          The address people use to reach this Moonphase. HTTPS is obtained for
          it automatically the first time someone visits by name.
        </p>
        <label>
          <span>Custom domain — optional</span>
          <input
            value={settings.public_url ?? ''}
            placeholder="moonphase.example.com"
            onChange={(e) =>
              setSettings({ ...settings, public_url: e.target.value })
            }
          />
        </label>
        <p className="hint" style={{ marginTop: -6 }}>
          Just the name is enough — <code>https://</code> is assumed, since that
          is what a certificate gives you. Leave it blank to keep using this
          machine&rsquo;s address.{' '}
          <a
            href="https://oliversvane.github.io/moonphase/guides/dns/"
            target="_blank"
            rel="noreferrer"
          >
            How to point a domain at it
          </a>
        </p>

        <label className="check">
          <input
            type="checkbox"
            checked={settings.signup_open}
            onChange={(e) =>
              setSettings({ ...settings, signup_open: e.target.checked })
            }
          />
          <span>Let other people create accounts</span>
        </label>
        <p className="hint" style={{ marginTop: -4 }}>
          With this off, only the accounts below can sign in — add people here
          instead.
        </p>

        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const saved = await instance.saveSettings(settings)
                setSettings(saved)
              }, 'Instance settings saved.')
            }
          >
            Save
          </button>
        </div>
      </div>

      <UpdatePanel busy={busy} />

      <div className="card inner">
        <h3>Ways to sign in</h3>
        <p className="hint">
          Changes take effect within a few seconds — nothing restarts.
        </p>
        <SignInMethods
          draft={methods}
          onChange={setMethods}
          redirectUri={redirectUri}
          domainMissing={domainMissing}
        />
        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const saved = await api.saveAuthMethods(methods)
                setMethods(draftFrom(saved))
              }, 'Sign-in methods saved.')
            }
          >
            Save
          </button>
        </div>
      </div>

    </div>
  )
}
