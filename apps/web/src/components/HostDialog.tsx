import { useState, type FormEvent } from 'react'
import { currentHost, fetchConfig, normaliseHost, rememberHost } from '../lib/host'

/**
 * Which Moonphase this app is pointed at.
 *
 * It used to be a button that forgot the host and reloaded, immediately, on the
 * first click — next to Settings and Sign out, at the bottom of the sidebar
 * where a misclick is cheap everywhere else. People hit it by accident and had
 * to find and retype their address to get back in.
 *
 * So it asks. Leaving the address alone and pressing Save changes nothing at
 * all, which is the property that makes the accident harmless: the dangerous
 * outcome now needs somebody to type a different address, and the address is
 * checked before anything is thrown away.
 */
export function HostDialog({ onClose }: { onClose: () => void }) {
  const existing = currentHost()
  const [host, setHost] = useState(existing)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()

    const next = normaliseHost(host)
    // Unchanged, or emptied and left that way: there is nothing to do, and
    // doing nothing is the whole point.
    if (!next || next === normaliseHost(existing)) {
      onClose()
      return
    }

    setBusy(true)
    setError(null)
    try {
      // Checked before the current host is given up. Switching to an address
      // that does not answer would leave the app with nowhere to go and no way
      // back except retyping the old one from memory.
      await fetchConfig(next)
      rememberHost(next)
      window.location.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>Moonphase host</h2>
        <p className="hint">
          The address this app talks to. Everything you see comes from here.
        </p>

        <form onSubmit={submit}>
          {error && <div className="banner error">{error}</div>}

          <label>
            <span>Address</span>
            <input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              placeholder="https://moonphase.example.com"
              autoFocus
            />
          </label>
          <p className="hint" style={{ marginTop: -6 }}>
            Leave it as it is and nothing changes. A different address is
            checked before switching, so a typo cannot strand you here.
          </p>

          <div className="actions">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Checking…' : 'Save'}
            </button>
            <div className="spacer" />
            <button type="button" onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
