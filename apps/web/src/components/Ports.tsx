import { useCallback, useEffect, useState } from 'react'
import { api, type DetectedPort, type PreviewService } from '../lib/api'
import { isDesktop, openPreviewWindow } from '../lib/desktop'

interface Props {
  projectId: string
  projectName: string
  running: boolean
  /**
   * Shared with view-only access. Ports still show — knowing what the agent
   * has running is part of watching it — but opening a tunnel puts the app on
   * the network, which is a decision for whoever owns the project.
   */
  readOnly?: boolean
}

/**
 * Whatever the container is listening on, discovered rather than declared.
 *
 * Polls while the project is running so a dev server started a minute ago —
 * or restarted onto a different port — simply appears.
 */
export function Ports({ projectId, projectName, running, readOnly = false }: Props) {
  const [ports, setPorts] = useState<DetectedPort[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<number | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [opening, setOpening] = useState(false)
  const [services, setServices] = useState<PreviewService[]>([])

  // Whatever you opened last, per project. The ranking below is a good guess
  // and a guess all the same; once you have corrected it, it should stay
  // corrected rather than being re-guessed every time.
  const rememberedKey = `moonphase.preview.${projectId}`
  const remember = (port: number) => {
    try {
      window.localStorage.setItem(rememberedKey, String(port))
    } catch {
      // Private browsing, or storage disabled. Losing the preference is fine.
    }
  }
  const remembered = (): number | null => {
    try {
      const value = window.localStorage.getItem(rememberedKey)
      return value ? Number(value) : null
    } catch {
      return null
    }
  }

  /**
   * Open the whole project, not a port.
   *
   * A forwarded port gives the browser one service at a renumbered address,
   * which is enough for a static site and useless for anything that calls its
   * own API: the page runs here, so `http://localhost:8000` means this
   * machine's port 8000. The preview window routes through a proxy that
   * resolves every address inside the container, so the app's own assumptions
   * hold and nothing has to be rewritten.
   */
  const preview = async (port?: number) => {
    setOpening(true)
    setError(null)
    try {
      const opened = await api.openPreview(projectId)
      setServices(opened.services)

      // Your last choice wins, as long as it is still listening. Otherwise the
      // server has already ordered these by what it found — whatever serves
      // HTML first, an API last — so the head of the list is the answer.
      const previous = remembered()
      const stillThere = opened.services.some((s) => s.port === previous)
      const target =
        port ?? (previous !== null && stillThere ? previous : opened.services[0]?.port)
      if (target === undefined) {
        throw new Error('Nothing is listening in this project yet.')
      }
      remember(target)
      await openPreviewWindow({
        projectId,
        projectName,
        proxyPort: opened.proxy_port,
        // The container's own address, which is the point: this is what the
        // app believes it is being served from.
        url: target === 80 ? 'http://localhost/' : `http://localhost:${target}/`,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setOpening(false)
    }
  }

  const refresh = useCallback(async () => {
    if (!running) return
    try {
      setPorts(await api.ports(projectId))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoaded(true)
    }
  }, [projectId, running])

  useEffect(() => {
    setLoaded(false)
    void refresh()
    if (!running) return
    const id = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(id)
  }, [refresh, running])

  const toggle = async (entry: DetectedPort) => {
    setPending(entry.port)
    setError(null)
    try {
      if (entry.shared) {
        await api.unsharePort(projectId, entry.port)
      } else {
        await api.sharePort(projectId, entry.port)
      }
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPending(null)
    }
  }

  if (!running) return null

  return (
    <div className="ports">
      <div className="ports-head">
        <span className="ports-title">Ports</span>
        {isDesktop() && !readOnly && (
          <button
            className="primary ports-preview"
            disabled={opening}
            title="Open this project in a window whose network is inside the container, so localhost means what the app thinks it means"
            onClick={() => void preview()}
          >
            {opening ? 'Opening…' : 'Preview'}
          </button>
        )}
        {error ? (
          <span className="ports-error" title={error}>
            unavailable
          </span>
        ) : ports.length === 0 ? (
          <span className="ports-empty">
            {loaded ? 'nothing listening yet' : 'looking…'}
          </span>
        ) : !isDesktop() && ports.length > 1 ? (
          // Worth saying rather than letting them find out: a forwarded port
          // renumbers, so an app that calls its own API by address will fail
          // here in a way that looks like the app is broken.
          <span
            className="ports-empty"
            title="A forwarded port is renumbered, so an app calling its own API at a fixed address cannot reach it. The desktop app previews through a proxy, where the container's own addresses resolve."
          >
            several services — open in the desktop app
          </span>
        ) : null}
      </div>

      {ports.map((entry) => (
        <div className="port-row" key={entry.port}>
          <span className={`dot${entry.shared ? ' connected' : ''}`} />
          <span className="port-number">{entry.port}</span>
          <span className="port-process">
            {services.find((s) => s.port === entry.port)?.title ??
              entry.process ??
              entry.bind}
          </span>

          {isDesktop() && !readOnly && (
            <button
              className="ghost"
              disabled={opening}
              title={`Open http://localhost:${entry.port} as the container sees it`}
              onClick={() => void preview(entry.port)}
            >
              open
            </button>
          )}
          {entry.shared && entry.url ? (
            <>
              <a
                className="port-link"
                href={entry.url}
                target="_blank"
                rel="noreferrer"
                title={entry.url}
              >
                open
              </a>
              {!readOnly && (
                <button
                  className="ghost"
                  disabled={pending === entry.port}
                  onClick={() => void toggle(entry)}
                >
                  stop
                </button>
              )}
            </>
          ) : (
            !readOnly && (
              <button
                className="ghost"
                disabled={pending === entry.port}
                onClick={() => void toggle(entry)}
              >
                {pending === entry.port ? '…' : 'share'}
              </button>
            )
          )}
        </div>
      ))}
    </div>
  )
}
