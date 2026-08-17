import { useState } from 'react'
import {
  fetchConfig,
  insecureHostWarning,
  normaliseHost,
  type InstanceConfig,
} from '../lib/host'

interface Props {
  /** Prefilled when a previous attempt failed, so it can be corrected. */
  initial?: string
  problem?: string | null
  onConnected: (host: string, config: InstanceConfig) => void
}

/**
 * Where is your Moonphase?
 *
 * Shown when the app cannot work out which host to talk to — which is exactly
 * once, the first time it is installed on a phone. Everything else about the
 * instance comes from `GET /api/config` on the address typed here, so this is
 * the only thing a person ever has to know.
 */
export function Connect({ initial, problem, onConnected }: Props) {
  const [value, setValue] = useState(initial ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(problem ?? null)

  const connect = async () => {
    const host = normaliseHost(value)
    if (!host) {
      setError('Enter the address of your Moonphase host.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const config = await fetchConfig(host)
      onConnected(host, config)
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : `Could not reach ${host}. Check the address and that it is running.`,
      )
    } finally {
      setBusy(false)
    }
  }

  const warning = value ? insecureHostWarning(normaliseHost(value)) : null

  return (
    <div className="auth-shell">
      <form
        className="card connect-card"
        onSubmit={(e) => {
          e.preventDefault()
          void connect()
        }}
      >
        <h2>
          <span className="glyph">◐</span> Connect to Moonphase
        </h2>
        <p className="hint">
          The address of the server you run Moonphase on. Everything else — where
          your account lives, which key signs notifications — comes from there.
        </p>

        <label htmlFor="host">Host</label>
        <input
          id="host"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="moonphase.example.com"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          inputMode="url"
          autoFocus
        />

        {warning && <div className="banner info">{warning}</div>}
        {error && <div className="banner error">{error}</div>}

        <button className="primary" type="submit" disabled={busy || !value.trim()}>
          {busy ? 'Connecting…' : 'Connect'}
        </button>

        <p className="hint">
          Assumed to be <code>https://</code> unless you say otherwise. Notifications
          and installing to your home screen both need it.
        </p>
      </form>
    </div>
  )
}
