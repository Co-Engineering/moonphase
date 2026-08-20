import { useEffect, useState, type FormEvent } from 'react'
import * as api from '../lib/api'
import { client } from '../lib/supabase'

export function Auth() {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Which ways in this instance actually offers. Drawing a button for a
  // provider that is not configured sends people to an error page belonging to
  // someone else.
  const [methods, setMethods] = useState<string[]>(['password'])
  // Whether anyone may sign up here. Assumed open until the instance says
  // otherwise, so a slow answer never hides the link on an instance that wants
  // it — the server refuses the attempt either way.
  const [canSignUp, setCanSignUp] = useState(true)

  useEffect(() => {
    let cancelled = false
    void api
      .authMethods()
      .then((found) => {
        if (!cancelled && found.enabled.length > 0) setMethods(found.enabled)
      })
      .catch(() => {
        // An instance too old to answer offers what it always did.
      })
    void api
      .signupOpen()
      .then((open) => {
        if (cancelled) return
        setCanSignUp(open)
        // Someone who was already on the form when it closed, or who arrived
        // by a stale link, should not be left typing into something that
        // cannot work.
        if (!open) setMode('signin')
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  const oauth = async (provider: 'google' | 'azure') => {
    setError(null)
    const { error: authError } = await client().auth.signInWithOAuth({
      provider,
      options: { redirectTo: window.location.origin },
    })
    if (authError) setError(authError.message)
  }

  const magicLink = async () => {
    if (!email) {
      setError('Enter your email first, and a link will be sent to it.')
      return
    }
    setBusy(true)
    setError(null)
    const { error: authError } = await client().auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin },
    })
    if (authError) setError(authError.message)
    else setNotice('Check your email — the link signs you in.')
    setBusy(false)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)

    const { error: authError, data } =
      mode === 'signin'
        ? await client().auth.signInWithPassword({ email, password })
        : await client().auth.signUp({ email, password })

    if (authError) {
      setError(authError.message)
    } else if (mode === 'signup' && !data.session) {
      // Only happens when email confirmation is enabled on the Supabase project.
      setNotice('Check your email to confirm the account, then sign in.')
      setMode('signin')
    }
    setBusy(false)
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="brand">
          <span className="glyph">◐</span> Moonphase
        </div>
        <p className="tagline">
          Your coding agents live on servers you own,
          <br />
          not on the laptop you need to close.
        </p>

        <div className="card">
          <form onSubmit={submit}>
            {error && <div className="banner error">{error}</div>}
            {notice && <div className="banner info">{notice}</div>}

            <label>
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </label>

            {methods.includes('password') && (
              <label>
                <span>Password</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
                  minLength={8}
                  required
                />
              </label>
            )}

            {methods.includes('password') && (
              <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
                {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
              </button>
            )}

            {methods.includes('magic_link') && (
              <button
                type="button"
                className={methods.includes('password') ? 'ghost' : 'primary'}
                disabled={busy}
                style={{ width: '100%' }}
                onClick={() => void magicLink()}
              >
                Email me a link
              </button>
            )}

            {(methods.includes('google') || methods.includes('microsoft')) && (
              <>
                {methods.includes('password') && <div className="or">or</div>}
                {methods.includes('google') && (
                  <button
                    type="button"
                    className="ghost provider"
                    disabled={busy}
                    onClick={() => void oauth('google')}
                  >
                    Continue with Google
                  </button>
                )}
                {methods.includes('microsoft') && (
                  <button
                    type="button"
                    className="ghost provider"
                    disabled={busy}
                    onClick={() => void oauth('azure')}
                  >
                    Continue with Microsoft
                  </button>
                )}
              </>
            )}
          </form>

          <div className="auth-toggle">
            {mode === 'signin' && !canSignUp ? (
              // Offering a link that can only fail is worse than saying so.
              <span className="hint">
                This Moonphase is not taking new accounts. Ask whoever runs it
                for an invitation.
              </span>
            ) : mode === 'signin' ? (
              <>
                No account?
                <button type="button" onClick={() => setMode('signup')}>
                  Create one
                </button>
              </>
            ) : (
              <>
                Already have one?
                <button type="button" onClick={() => setMode('signin')}>
                  Sign in
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
