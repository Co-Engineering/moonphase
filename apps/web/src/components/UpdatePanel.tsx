import { useCallback, useEffect, useState } from 'react'
import { instance, type UpdateState } from '../lib/api'
import { copyText } from '../lib/clipboard'

/**
 * Which build this is, and whether a newer one has been released.
 *
 * The button only appears when there is something to apply — a screen that
 * offers "Update" to somebody already on the newest release teaches people to
 * ignore it. Everything else here is a statement of fact: the version running,
 * and, when an update cannot be applied from the app, the command that does it.
 *
 * Three states rather than two. "Cannot say" — GitHub unreachable, no releases
 * published, a development build — is not "up to date", and showing it as such
 * would be a lie somebody acts on.
 */
export function UpdatePanel({ busy }: { busy: boolean }) {
  const [state, setState] = useState<UpdateState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const [copied, setCopied] = useState(false)
  // Set the moment the request is accepted, and never cleared by a failed
  // poll. Asking for an update returns before the updater has noticed it —
  // it looks for the request every few seconds — so `status` is still
  // whatever it was, and waiting for it to say "running" before showing
  // anything left the screen looking like the button had done nothing.
  const [requested, setRequested] = useState(false)

  const load = useCallback(async (force = false) => {
    setError(null)
    try {
      const next = await instance.update(force)
      setState(next)
      // The updater has reported an outcome, so this is over either way.
      if (next.status === 'ok' || next.status === 'failed') setRequested(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // While an update is running this instance is being replaced underneath the
  // page, so requests fail for a few seconds. Polling through that is the only
  // way to see how it ended: the request that started it does not survive.
  useEffect(() => {
    if (!requested && state?.status !== 'running') return
    const id = window.setInterval(() => void load(), 3000)
    return () => window.clearInterval(id)
  }, [requested, state?.status, load])

  if (error && !requested) return <div className="banner error">{error}</div>
  if (!state) {
    // Mid-update the API is being replaced, so there is nothing to report and
    // saying so beats an error the person is meant to ignore.
    if (requested) {
      return (
        <div className="card inner">
          <h3>Version</h3>
          <div className="banner">
            <strong>Updating</strong> — the services are restarting, so this
            page has lost contact for a moment. It reconnects on its own.
          </div>
        </div>
      )
    }
    return <p className="hint">Checking for updates…</p>
  }

  const running = state.running_version ?? 'development build'
  const outdated = state.update_available === true

  return (
    <div className="card inner">
      <h3>Version</h3>

      {/* The same two-column list the project panel uses for its facts. */}
      <dl className="meta-grid" style={{ marginBottom: 12 }}>
        <dt>Running</dt>
        <dd>
          {running}
          {state.running_commit && (
            <span className="hint"> · {state.running_commit.slice(0, 8)}</span>
          )}
        </dd>
        {state.latest_version && (
          <>
            <dt>Latest release</dt>
            <dd>
              {state.release_url ? (
                <a href={state.release_url} target="_blank" rel="noreferrer">
                  {state.latest_version}
                </a>
              ) : (
                state.latest_version
              )}
            </dd>
          </>
        )}
      </dl>

      {(requested || state.status === 'running') && state.status !== 'failed' && (
        <div className="banner">
          <strong>Updating</strong> — {state.status_detail ?? 'asking the updater'}.
          {' '}This page will lose contact while the services restart, which is
          expected; it reconnects on its own.
        </div>
      )}
      {state.status === 'failed' && (
        <div className="banner error">
          The last update failed: {state.status_detail ?? 'no reason given'}
        </div>
      )}

      {outdated ? (
        <>
          <div className="banner">
            <strong>{state.latest_version}</strong> is available.
            {state.release_notes && (
              <details className="pane-details" style={{ marginTop: 8 }}>
                <summary>What changed</summary>
                <pre className="pane">{state.release_notes}</pre>
              </details>
            )}
          </div>

          {state.can_apply ? (
            <div className="actions">
              <button
                className="primary"
                disabled={busy || working || state.status === 'running'}
                onClick={() =>
                  void (async () => {
                    setWorking(true)
                    setError(null)
                    try {
                      const next = await instance.applyUpdate()
                      // Before setting state, so the panel is already showing
                      // that something is happening whatever the updater has
                      // had time to write.
                      setRequested(true)
                      setState(next)
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err))
                    } finally {
                      setWorking(false)
                    }
                  })()
                }
              >
                {working ? 'Starting…' : `Update to ${state.latest_version}`}
              </button>
            </div>
          ) : (
            <>
              <p className="hint">
                Run this on the server to update. One-click updates need an
                updater alongside Moonphase, which is opt-in because it is the
                one component that can reach the host&rsquo;s Docker daemon —{' '}
                <a
                  href="https://co-engineering.github.io/moonphase/guides/updating/"
                  target="_blank"
                  rel="noreferrer"
                >
                  how to turn it on
                </a>
                .
              </p>
              <div className="keyblock">{state.command}</div>
              <div className="actions">
                <button
                  onClick={() =>
                    void copyText(state.command).then((ok) => setCopied(ok))
                  }
                >
                  {copied ? 'Copied' : 'Copy command'}
                </button>
              </div>
            </>
          )}
        </>
      ) : (
        <p className="hint">
          {state.update_available === false
            ? 'This is the latest release.'
            : (state.detail ?? 'Could not check for updates.')}
        </p>
      )}

      <div className="actions">
        <button disabled={busy} onClick={() => void load(true)}>
          Check again
        </button>
      </div>
    </div>
  )
}
