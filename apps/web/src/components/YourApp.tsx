import { useCallback, useEffect, useState } from 'react'
import { api, type DetectedPort, type PreviewService } from '../lib/api'
import { isDesktop, openPreviewWindow } from '../lib/desktop'

/**
 * The thing you made, with a button that opens it.
 *
 * The backend already works out which port serves a page, what its title is,
 * and which one a person most likely meant — and the old strip threw all of
 * that away to render "5173 ::1 · share". For someone who does not know what a
 * port is, the payoff moment of this entire product was a row of numbers.
 *
 * So: one obvious button for the app itself, plain names for everything else,
 * and sharing described as what it does rather than what it is called. The
 * numbers stay, in small type, because they stop being noise the moment you do
 * know what they mean.
 */

interface Props {
  projectId: string
  projectName: string
  running: boolean
  /**
   * Shared with view-only access. Services still show — knowing what the agent
   * has running is part of watching it — but putting one on the network is a
   * decision for whoever owns the project.
   */
  readOnly?: boolean
}

/**
 * What to call a service in front of someone who did not choose its port.
 *
 * The page's own <title> is by far the best name available: it is what the
 * person typed into their own app. Everything else is a fallback.
 */
export function label(service: PreviewService | null | undefined, port: number): string {
  const title = service?.title?.trim()
  if (title && !isAutoindex(title)) return title
  if (service?.kind === 'page') return 'Your app'
  if (service?.kind === 'api') return 'Data service'
  return `Something on port ${port}`
}

/**
 * Which of these is the one to put behind the big button.
 *
 * A page that has given itself a title beats one that has not. Both are HTML
 * and the server ranks them by port, which picked the wrong one the moment a
 * project ran two dev servers: the untitled page on the lower port was a
 * placeholder, and the app someone actually built was next door calling itself
 * "Todo". Landing on the placeholder looks exactly like the app is broken.
 */
export function primary(services: PreviewService[]): PreviewService | null {
  const pages = services.filter((s) => s.kind === 'page')
  const named = pages.find((s) => Boolean(s.title?.trim()) && !isAutoindex(s.title))
  return named ?? pages[0] ?? services[0] ?? null
}

function isAutoindex(title: string | null | undefined): boolean {
  return /^(directory listing|index of)\b/i.test((title ?? '').trim())
}

export function YourApp({ projectId, projectName, running, readOnly = false }: Props) {
  const [ports, setPorts] = useState<DetectedPort[]>([])
  const [services, setServices] = useState<PreviewService[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<number | null>(null)
  const [opening, setOpening] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const found = await api.ports(projectId)
      setPorts(found)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoaded(true)
    }
  }, [projectId])

  useEffect(() => {
    if (!running) return
    void refresh()
    const timer = window.setInterval(() => void refresh(), 6000)
    return () => window.clearInterval(timer)
  }, [running, refresh])

  useEffect(() => {
    if (!running || ports.length === 0) return
    let cancelled = false
    void api
      .openPreview(projectId)
      .then((found) => {
        if (!cancelled) setServices(found.services)
      })
      .catch(() => {
        // The probe is a nicety; the numbers below still work without it.
      })
    return () => {
      cancelled = true
    }
  }, [projectId, running, ports.length])

  async function open(port?: number) {
    setOpening(true)
    setError(null)
    try {
      const found = await api.openPreview(projectId)
      await openPreviewWindow({
        projectId,
        projectName,
        proxyPort: found.proxy_port,
        url: `http://localhost:${port ?? primary(found.services)?.port ?? 3000}`,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setOpening(false)
    }
  }

  async function toggleLink(entry: DetectedPort) {
    setPending(entry.port)
    setError(null)
    try {
      if (entry.shared) await api.unsharePort(projectId, entry.port)
      else await api.sharePort(projectId, entry.port)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending(null)
    }
  }

  if (!running) return null

  const top = primary(services)
  const topPort = top?.port ?? ports[0]?.port
  const shared = ports.filter((entry) => entry.shared && entry.url)

  return (
    <div className="your-app">
      <div className="your-app-head">
        <span className="your-app-title">Your app</span>

        {ports.length === 0 ? (
          <span className="hint">
            {loaded
              ? 'Nothing is running yet — ask Claude to start it.'
              : 'Looking for it…'}
          </span>
        ) : (
          <>
            {isDesktop() && !readOnly && topPort !== undefined && (
              <button
                className="primary"
                disabled={opening}
                onClick={() => void open(topPort)}
                title="Opens in a window whose network is inside the container, so the app works exactly as it does there"
              >
                {opening ? 'Opening…' : `Open ${label(top, topPort)}`}
              </button>
            )}
            {!isDesktop() && (
              // Worth saying plainly rather than letting them find out: a
              // forwarded port is renumbered, so an app that calls its own API
              // by address fails here in a way that looks like a broken app.
              <span className="hint">
                Open the desktop app to see it working properly.
              </span>
            )}
            <button className="link" onClick={() => setExpanded((on) => !on)}>
              {expanded ? 'Hide details' : `${ports.length} running`}
            </button>
          </>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {shared.length > 0 && (
        <div className="public-links">
          {shared.map((entry) => (
            <div className="public-link" key={entry.port}>
              <span className="dot connected" />
              <span>
                {label(
                  services.find((s) => s.port === entry.port),
                  entry.port,
                )}{' '}
                is public
              </span>
              <a href={entry.url ?? '#'} target="_blank" rel="noreferrer">
                {entry.url}
              </a>
              {!readOnly && (
                <button
                  className="ghost small"
                  disabled={pending === entry.port}
                  onClick={() => void toggleLink(entry)}
                >
                  Stop sharing
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {expanded && (
        <div className="service-list">
          {ports.map((entry) => {
            const service = services.find((s) => s.port === entry.port)
            return (
              <div className="service-row" key={entry.port}>
                <span className={`dot${entry.shared ? ' connected' : ''}`} />
                <span className="service-name">{label(service, entry.port)}</span>
                <span className="service-port">port {entry.port}</span>
                {isDesktop() && !readOnly && (
                  <button
                    className="ghost small"
                    disabled={opening}
                    onClick={() => void open(entry.port)}
                  >
                    Open
                  </button>
                )}
                {!readOnly && !entry.shared && (
                  <button
                    className="ghost small"
                    disabled={pending === entry.port}
                    onClick={() => void toggleLink(entry)}
                    title="Anyone with the link can reach it, with no password"
                  >
                    {pending === entry.port ? '…' : 'Get a public link'}
                  </button>
                )}
              </div>
            )
          })}
          <p className="hint">
            A public link works for anyone who has it, with no password. Stop sharing when
            you are done showing someone.
          </p>
        </div>
      )}
    </div>
  )
}
