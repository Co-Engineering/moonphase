import { useEffect, useRef, useState } from 'react'
import { api, type McpOAuthConnection } from '../lib/api'
import { copyText } from '../lib/clipboard'

/**
 * Which running session carries the relay. The resulting credential is
 * org-wide regardless of which one actually ran it, so "session" is the only
 * scope that pins one down in advance — "project" and "org" hand that choice
 * to the backend, which picks any one of the caller's own running sessions
 * in reach and reports back which it used.
 */
export type McpConnectTarget =
  | { scope: 'session'; projectId: string; session: string }
  | { scope: 'project'; projectId: string }
  | { scope: 'org' }

function startFor(target: McpConnectTarget, serverName: string) {
  switch (target.scope) {
    case 'session':
      return api.startMcpOAuth(target.projectId, target.session, serverName)
    case 'project':
      return api.startMcpOAuthForProject(target.projectId, serverName)
    case 'org':
      return api.startMcpOAuthForOrg(serverName)
  }
}

/**
 * Relaying OAuth for one MCP server, inside a running session.
 *
 * Claude Code's own OAuth for an MCP server redirects to
 * `http://localhost:PORT/callback` — meaningless from a container on a
 * remote server, and no tunnel can fix it, because the provider genuinely
 * sends the browser there. `--no-browser` mode also accepts the resulting
 * redirect URL typed back at a prompt, so completing it never actually
 * requires that listener to be reachable — only that the link gets opened,
 * and whatever the browser lands on gets pasted back here. Same shape as
 * signing in to Claude itself, one server at a time instead of the account.
 */
export function McpConnectDialog({
  target,
  serverName,
  onClose,
  onConnected,
}: {
  target: McpConnectTarget
  serverName: string
  onClose: () => void
  onConnected: () => void
}) {
  const [connection, setConnection] = useState<McpOAuthConnection | null>(null)
  const [redirectUrl, setRedirectUrl] = useState('')
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState<'copied' | 'failed' | null>(null)
  const pollRef = useRef<number | undefined>(undefined)
  const startedRef = useRef(false)

  useEffect(() => () => window.clearInterval(pollRef.current), [])

  const start = async () => {
    setWorking(true)
    setError(null)
    try {
      const started = await startFor(target, serverName)
      setConnection(started)
      if (started.state === 'error') {
        setError(started.detail ?? 'Could not start.')
        setWorking(false)
        return
      }
      // Whichever session actually carried it — settled by the backend for
      // 'project'/'org' targets, so every poll and paste from here on uses
      // this rather than any project id the caller happened to start with.
      const projectId = started.project_id
      window.clearInterval(pollRef.current)
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api.pollMcpOAuth(projectId, started.session_id)
          setConnection(next)
          if (next.state === 'starting') return
          window.clearInterval(pollRef.current)
          setWorking(false)
          if (next.state === 'error') {
            setError(next.detail ?? 'Could not start the connection.')
          }
        } catch (err) {
          window.clearInterval(pollRef.current)
          setWorking(false)
          setError(err instanceof Error ? err.message : String(err))
        }
      }, 1500)
    } catch (err) {
      setWorking(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  // Starts itself the moment the dialog opens — there is nothing else to
  // configure first, unlike the account flow which offers an API key
  // alternative up front.
  useEffect(() => {
    if (startedRef.current) return
    startedRef.current = true
    void start()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = async () => {
    if (!connection || !redirectUrl.trim()) return
    setWorking(true)
    setError(null)
    try {
      const projectId = connection.project_id
      const started = await api.pasteMcpOAuth(
        projectId,
        connection.session_id,
        redirectUrl.trim(),
      )
      setConnection(started)
      window.clearInterval(pollRef.current)
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await api.pollMcpOAuth(projectId, started.session_id)
          setConnection(next)
          if (next.state === 'complete' || next.state === 'error') {
            window.clearInterval(pollRef.current)
            setWorking(false)
            if (next.state === 'complete') {
              onConnected()
            } else {
              setError(next.detail ?? 'Could not complete the connection.')
            }
          }
        } catch (err) {
          window.clearInterval(pollRef.current)
          setWorking(false)
          setError(err instanceof Error ? err.message : String(err))
        }
      }, 1500)
    } catch (err) {
      setWorking(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const verifying = connection?.state === 'verifying'
  const awaitingPaste = connection?.state === 'awaiting_paste' || verifying
  const done = connection?.state === 'complete'

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>Connect {serverName}</h2>
        <p className="hint">
          {target.scope === 'session'
            ? 'Runs the connection through this session'
            : 'Runs the connection through one of your own running sessions'}{' '}
          — its own OAuth callback points at a port on that container, which your
          browser cannot reach directly, so this relays it the way signing in to
          Claude itself already works: open the link, then paste back wherever your
          browser ends up.
        </p>

        {error && <div className="banner error">{error}</div>}
        {done && <div className="banner">Connected. Every session in this org can use it now.</div>}

        {(verifying || error) && connection?.pane && (
          <details className="pane-details" open={Boolean(error)}>
            <summary>Terminal output</summary>
            <pre className="pane">{connection.pane}</pre>
          </details>
        )}

        {!connection && !error && <p className="hint">Starting…</p>}

        {awaitingPaste && connection && (
          <>
            <p className="hint">
              <strong>1.</strong> Open this URL and approve:
            </p>
            <div className="keyblock">
              <a href={connection.url ?? ''} target="_blank" rel="noreferrer">
                {connection.url}
              </a>
            </div>
            <div className="actions" style={{ marginBottom: 12 }}>
              <button
                onClick={() =>
                  void copyText(connection.url ?? '').then((ok) =>
                    setCopied(ok ? 'copied' : 'failed'),
                  )
                }
              >
                {copied === 'copied' ? 'Copied' : 'Copy URL'}
              </button>
              {copied === 'failed' && (
                <span className="hint">Could not copy — select the link above and copy it.</span>
              )}
            </div>
            <p className="hint">
              <strong>2.</strong> It will fail to load once you approve — that is
              expected. Paste the URL from your browser's address bar anyway:
            </p>
            <label>
              <input
                value={redirectUrl}
                onChange={(e) => setRedirectUrl(e.target.value)}
                placeholder="http://localhost:..."
                disabled={verifying}
                autoFocus
              />
            </label>
            <div className="actions">
              <button
                className="primary"
                disabled={working || verifying || !redirectUrl.trim()}
                onClick={() => void submit()}
              >
                {verifying ? 'Waiting…' : 'Finish connecting'}
              </button>
              <button onClick={onClose}>Cancel</button>
            </div>
          </>
        )}

        {!awaitingPaste && !done && (
          <div className="actions">
            {!error && <button disabled className="primary">Starting…</button>}
            {error && (
              <button className="primary" disabled={working} onClick={() => void start()}>
                Try again
              </button>
            )}
            <button onClick={onClose}>Close</button>
          </div>
        )}

        {done && (
          <div className="actions">
            <button className="primary" onClick={onClose}>
              Done
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
