import { useState, type FormEvent } from 'react'
import * as api from '../lib/api'
import { client } from '../lib/supabase'

/**
 * First run.
 *
 * Installing something should not end with "now edit a file on the server".
 * The address this answers on and whether anyone else may sign up are decisions
 * you make after seeing it work, so they are made here and stored in the
 * database — where they can be changed later without a shell.
 *
 * Two steps, because they are two different kinds of thing: who you are, and
 * what this instance is. The first account is the owner by construction — there
 * is nobody else yet — and signup closes behind it unless you say otherwise.
 */

interface Props {
  onDone: () => void
}

export function Setup({ onDone }: Props) {
  const [step, setStep] = useState<'account' | 'instance'>('account')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  // Wherever this page was loaded from is almost always the right answer, and
  // typing it again is a chance to get it wrong.
  const [address, setAddress] = useState(() => window.location.origin)
  const [signupOpen, setSignupOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function createAccount(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const { error: authError, data } = await client().auth.signUp({ email, password })
    if (authError) {
      setError(authError.message)
    } else if (!data.session) {
      setError('The account was created but not signed in. Try signing in.')
    } else {
      setStep('instance')
    }
    setBusy(false)
  }

  async function finish(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.completeSetup({
        public_url: address.trim() || null,
        signup_open: signupOpen,
      })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="brand">
          <span className="glyph">◐</span> Moonphase
        </div>

        {step === 'account' ? (
          <>
            <p className="tagline">
              Nobody has an account here yet.
              <br />
              The first one is yours, and it owns this instance.
            </p>
            <div className="card">
              <form onSubmit={createAccount}>
                {error && <div className="banner error">{error}</div>}
                <label>
                  <span>Email</span>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    autoComplete="email"
                    required
                    autoFocus
                  />
                </label>
                <label>
                  <span>Password</span>
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                    minLength={8}
                    required
                  />
                </label>
                <button className="primary" disabled={busy}>
                  {busy ? 'Creating…' : 'Create my account'}
                </button>
              </form>
            </div>
          </>
        ) : (
          <>
            <p className="tagline">
              One more thing, and it is the only one.
            </p>
            <div className="card">
              <form onSubmit={finish}>
                {error && <div className="banner error">{error}</div>}

                <label>
                  <span>The address people will use</span>
                  <input
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                    placeholder="https://moonphase.example.com"
                    autoFocus
                  />
                </label>
                <p className="hint">
                  Point DNS at this machine and enter the address here. A certificate
                  is obtained automatically the first time someone visits — there is
                  nothing to install and nothing to renew.
                </p>
                <p className="hint">
                  Notifications need HTTPS, so until this is a real name you will not
                  be told when an agent is waiting for you.
                </p>

                <label className="check">
                  <input
                    type="checkbox"
                    checked={signupOpen}
                    onChange={(e) => setSignupOpen(e.target.checked)}
                  />
                  <span>Let other people create accounts</span>
                </label>
                <p className="hint">
                  Off is the safe default. Anyone who signs up gets their own empty
                  organization and can see nothing of yours, but it is still your
                  machine they are on. You can share servers and projects by email
                  either way.
                </p>

                <button className="primary" disabled={busy}>
                  {busy ? 'Saving…' : 'Finish'}
                </button>
              </form>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
