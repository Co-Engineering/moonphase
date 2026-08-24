import { useCallback, useEffect, useState } from 'react'
import { instance, type InstanceSettings, type Person } from '../lib/api'
import * as api from '../lib/api'
import { SignInMethods, draftFrom, type Draft } from './SignInMethods'
import { copyText } from '../lib/clipboard'
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
  const [people, setPeople] = useState<Person[] | null>(null)
  const [methods, setMethods] = useState<Draft>(() => draftFrom(null))
  const [redirectUri, setRedirectUri] = useState('')
  const [email, setEmail] = useState('')
  const [asAdmin, setAsAdmin] = useState(false)
  const [created, setCreated] = useState<{ email: string; password: string } | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [found, list, auth] = await Promise.all([
        instance.settings(),
        instance.people(),
        api.authMethods(),
      ])
      setSettings(found)
      setPeople(list)
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
  if (!settings || !people) return <p className="hint">Loading…</p>

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

      <div className="card inner">
        <h3>People</h3>
        <p className="hint">
          Everyone with an account here. An administrator can change these
          settings and manage accounts; everyone else just uses the thing.
        </p>

        {created && (
          // Shown once and never again: nothing stores it, and there is nowhere
          // to look it up afterwards.
          <div className="banner">
            <strong>{created.email}</strong> can sign in with this password.
            Copy it now — it is not stored anywhere and cannot be shown again.
            <div className="keyblock" style={{ marginTop: 8 }}>
              {created.password}
            </div>
            <div className="actions" style={{ marginTop: 8 }}>
              <button
                onClick={() =>
                  void copyText(created.password).then((ok) => {
                    if (!ok) {
                      setError('Could not copy. Select the password and copy it.')
                    }
                  })
                }
              >
                Copy password
              </button>
              <button onClick={() => setCreated(null)}>Done</button>
            </div>
          </div>
        )}

        <div className="person-list">
          {people.map((person) => (
            <div className="person" key={person.id}>
              <span className="person-email">{person.email}</span>
              {person.is_admin && <span className="shared-tag">admin</span>}
              {person.is_you && <span className="hint">you</span>}
              <div className="spacer" />
              {!person.is_you && (
                <>
                  <button
                    disabled={busy}
                    title={
                      person.is_admin
                        ? 'Take away administration of this instance'
                        : 'Let them change these settings and manage accounts'
                    }
                    onClick={() =>
                      void run(async () => {
                        await instance.setAdmin(person.id, !person.is_admin)
                        await load()
                      })
                    }
                  >
                    {person.is_admin ? 'Remove admin' : 'Make admin'}
                  </button>
                  <button
                    className="ghost danger"
                    disabled={busy}
                    title={
                      person.owned_projects > 0
                        ? `Owns ${person.owned_projects} project(s) — delete those first`
                        : 'Delete this account'
                    }
                    onClick={() =>
                      void run(async () => {
                        await instance.remove(person.id)
                        await load()
                      })
                    }
                  >
                    remove
                  </button>
                </>
              )}
            </div>
          ))}
        </div>

        <div className="new-person">
          <input
            value={email}
            placeholder="colleague@example.com"
            onChange={(e) => setEmail(e.target.value)}
          />
          <label className="check">
            <input
              type="checkbox"
              checked={asAdmin}
              onChange={(e) => setAsAdmin(e.target.checked)}
            />
            <span>Administrator</span>
          </label>
          <button
            className="primary"
            disabled={busy || !email.trim()}
            onClick={() =>
              void run(async () => {
                const person = await instance.invite(email.trim(), asAdmin)
                setCreated({ email: person.email, password: person.password })
                setEmail('')
                setAsAdmin(false)
                await load()
              })
            }
          >
            Add person
          </button>
        </div>
        <p className="hint">
          Creates the account and gives you a password to pass on. They can
          change it once they are in.
        </p>
      </div>
    </div>
  )
}
