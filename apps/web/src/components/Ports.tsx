import { useCallback, useEffect, useState } from 'react'
import { api, type DetectedPort } from '../lib/api'

interface Props {
  projectId: string
  running: boolean
}

/**
 * Whatever the container is listening on, discovered rather than declared.
 *
 * Polls while the project is running so a dev server started a minute ago —
 * or restarted onto a different port — simply appears.
 */
export function Ports({ projectId, running }: Props) {
  const [ports, setPorts] = useState<DetectedPort[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<number | null>(null)
  const [loaded, setLoaded] = useState(false)

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
        {error ? (
          <span className="ports-error" title={error}>
            unavailable
          </span>
        ) : ports.length === 0 ? (
          <span className="ports-empty">
            {loaded ? 'nothing listening yet' : 'looking…'}
          </span>
        ) : null}
      </div>

      {ports.map((entry) => (
        <div className="port-row" key={entry.port}>
          <span className={`dot${entry.shared ? ' connected' : ''}`} />
          <span className="port-number">{entry.port}</span>
          <span className="port-process">{entry.process ?? entry.bind}</span>

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
              <button
                className="ghost"
                disabled={pending === entry.port}
                onClick={() => void toggle(entry)}
              >
                stop
              </button>
            </>
          ) : (
            <button
              className="ghost"
              disabled={pending === entry.port}
              onClick={() => void toggle(entry)}
            >
              {pending === entry.port ? '…' : 'share'}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
