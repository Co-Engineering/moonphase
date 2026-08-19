import { useState, type FormEvent } from 'react'
import * as api from '../lib/api'
import { client } from '../lib/supabase'
import { SignInMethods, draftFrom, type Draft } from '../components/SignInMethods'

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
  const [step, setStep] = useState<'account' | 'instance' | 'methods'>('account')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  // Wherever this page was loaded from is almost always the right answer, and
  // typing it again is a chance to get it wrong.
  // Blank means "use whatever address this was reached on", which is the IP on
  // a machine with no DNS yet. Stored either way, because the auth service
  // builds its links from it.
  const [domain, setDomain] = useState('')
  const [signupOpen, setSignupOpen] = useState(false)
  const [methods, setMethods] = useState<Draft>(() => draftFrom(null))
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

  async function saveInstance(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // Saved before the sign-in step, because the redirect URI those providers
      // need is built from this address.
      await api.completeSetup({
        public_url: domain.trim() || window.location.origin,
        signup_open: signupOpen,
      })
      setStep('methods')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function finish(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const saved = await api.saveAuthMethods({
        ...methods,
        // Blank means "keep what is stored", which is what a form that cannot
        // show a secret back has to mean.
        google_client_secret: methods.google_client_secret || null,
        microsoft_client_secret: methods.microsoft_client_secret || null,
        smtp_password: methods.smtp_password || null,
      })
      if (saved.problems.length > 0) {
        setError(saved.problems.join(' '))
        setBusy(false)
        return
      }
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  const steps: Array<typeof step> = ['account', 'instance', 'methods']
  const at = steps.indexOf(step)

  return (
    <div className="auth-shell">
      <div className="auth-card setup">
        <div className="brand">
          <span className="glyph">◐</span> Moonphase
        </div>

        <ol className="steps" aria-label={`Step ${at + 1} of ${steps.length}`}>
          {['Account', 'Address', 'Signing in'].map((name, index) => (
            <li key={name} className={index === at ? 'now' : index < at ? 'done' : ''}>
              <span className="dot" />
              {name}
            </li>
          ))}
        </ol>

        {step === 'account' && (
          <>
            <p className="tagline">The first account owns this instance.</p>
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
                <p className="hint">At least 8 characters.</p>
                <button className="primary" disabled={busy}>
                  {busy ? 'Creating…' : 'Create my account'}
                </button>
              </form>
            </div>
          </>
        )}

        {step === 'instance' && (
          <>
            <p className="tagline">Where people will reach it.</p>
            <div className="card">
              <form onSubmit={saveInstance}>
                {error && <div className="banner error">{error}</div>}

                <label>
                  <span>Custom domain — optional</span>
                  <input
                    value={domain}
                    onChange={(e) => setDomain(e.target.value)}
                    placeholder="https://moonphase.example.com"
                    autoFocus
                  />
                </label>
                <p className="hint">
                  Blank keeps <code>{window.location.host}</code> on plain HTTP. A
                  domain adds HTTPS automatically, and is required for notifications
                  and for signing in with Google or Microsoft.{' '}
                  <a
                    href="https://oliversvane.github.io/moonphase/guides/dns/"
                    target="_blank"
                    rel="noreferrer"
                  >
                    How to add the DNS record →
                  </a>
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
                  Off means only you. Servers and projects can still be shared by
                  email.
                </p>

                <div className="actions">
                  <button className="primary" disabled={busy}>
                    {busy ? 'Saving…' : 'Next'}
                  </button>
                </div>
              </form>
            </div>
          </>
        )}

        {step === 'methods' && (
          <>
            <p className="tagline">How people sign in. All of it is changeable later.</p>
            <div className="card">
              <form onSubmit={finish}>
                {error && <div className="banner error">{error}</div>}
                <SignInMethods
                  draft={methods}
                  onChange={setMethods}
                  redirectUri={`${(domain.trim() || window.location.origin).replace(/\/$/, '')}/auth/v1/callback`}
                  domainMissing={!domain.trim()}
                />
                <div className="actions">
                  <button type="button" className="ghost" onClick={() => setStep('instance')}>
                    Back
                  </button>
                  <button className="primary" disabled={busy}>
                    {busy ? 'Saving…' : 'Finish'}
                  </button>
                </div>
              </form>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
