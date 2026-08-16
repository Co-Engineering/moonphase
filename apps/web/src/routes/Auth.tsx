import { useState, type FormEvent } from 'react'
import { supabase } from '../lib/supabase'

export function Auth() {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)

    const { error: authError, data } =
      mode === 'signin'
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password })

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

            <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
              {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <div className="auth-toggle">
            {mode === 'signin' ? (
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
