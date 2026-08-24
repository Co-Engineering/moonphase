import { useCallback, useEffect, useState } from 'react'
import { instance, type Person } from '../lib/api'
import { copyText } from '../lib/clipboard'

/**
 * Who has an account on this instance.
 *
 * Its own tab rather than a card inside Instance, where it sat next to the
 * domain and the sign-in methods. Those are settings; this is a list of people,
 * and looking after colleagues is not a detail of the instance's address.
 */
export function PeopleTab({
  busy,
  run,
}: {
  busy: boolean
  run: (fn: () => Promise<unknown>, message?: string) => Promise<void>
}) {
  const [people, setPeople] = useState<Person[] | null>(null)
  const [email, setEmail] = useState('')
  const [asAdmin, setAsAdmin] = useState(false)
  const [created, setCreated] = useState<{ email: string; password: string } | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setPeople(await instance.people())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (error) return <div className="banner error">{error}</div>
  if (!people) return <p className="hint">Loading…</p>

  return (
    <div className="tab-body">
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
