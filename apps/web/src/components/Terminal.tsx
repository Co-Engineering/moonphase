import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { terminalUrl } from '../lib/api'

type Status = 'connecting' | 'attached' | 'disconnected' | 'error'

interface Props {
  projectId: string
  /** Which tmux session to attach to. Changing it reattaches. */
  session?: string
  onStatusChange?: (status: Status, detail?: string) => void
  /**
   * Someone else's session: their agent, their account. The server drops our
   * keystrokes either way, but a terminal that silently swallows typing is the
   * most confusing thing a terminal can do — so we stop them here and say so.
   */
  readOnly?: boolean
  /** Called when a keystroke was refused, so the caller can react visibly. */
  onRefusedInput?: () => void
}

/**
 * A live view onto the project's tmux session.
 *
 * Closing this component detaches; it never kills the remote session. That is
 * enforced on the server, but it is why reconnecting is cheap and why we
 * reconnect automatically rather than asking the user to.
 */
export function ProjectTerminal({
  projectId,
  session,
  onStatusChange,
  readOnly = false,
  onRefusedInput,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const termRef = useRef<Terminal | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  // Survives re-renders so a burst of failures backs off instead of hammering.
  const retryRef = useRef(0)
  const disposedRef = useRef(false)

  const [status, setStatus] = useState<Status>('connecting')

  // Read through a ref inside the xterm callback: the terminal is rebuilt only
  // when the project or session changes, so a plain closure over the prop
  // would still be sending keystrokes after access changed under it.
  const readOnlyRef = useRef(readOnly)
  readOnlyRef.current = readOnly
  const refusedRef = useRef(onRefusedInput)
  refusedRef.current = onRefusedInput

  useEffect(() => {
    onStatusChange?.(status)
  }, [status, onStatusChange])

  useEffect(() => {
    if (!hostRef.current) return
    disposedRef.current = false

    const term = new Terminal({
      fontFamily:
        '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: !readOnly,
      allowProposedApi: true,
      scrollback: 0, // tmux owns scrollback; a second buffer only fights it
      theme: {
        background: '#0f1017',
        foreground: '#c8d0e0',
        cursor: '#7aa2f7',
        selectionBackground: '#283457',
        black: '#15161e',
        red: '#f7768e',
        green: '#9ece6a',
        yellow: '#e0af68',
        blue: '#7aa2f7',
        magenta: '#bb9af7',
        cyan: '#7dcfff',
        white: '#a9b1d6',
      },
    })

    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(hostRef.current)

    /**
     * FitAddon reads the renderer's `dimensions`, which do not exist until the
     * terminal has painted once — and no longer exist after dispose. Calling
     * it too early (hidden pane, first frame) or too late (StrictMode's
     * double-mount disposing the first instance) throws a TypeError that
     * escapes as an unhandled error. Every fit goes through here.
     */
    const safeFit = () => {
      if (disposedRef.current) return
      try {
        fit.fit()
      } catch {
        // Not yet measurable, or already gone. Either way there is nothing to
        // resize and the next observer tick will retry.
      }
    }

    safeFit()

    termRef.current = term
    fitRef.current = fit

    let reconnectTimer: number | undefined

    const connect = async () => {
      if (disposedRef.current) return
      setStatus('connecting')

      const url = await terminalUrl(projectId, term.cols, term.rows, session)
      // terminalUrl awaits the auth token; the component may have unmounted
      // in the meantime, and opening a socket now would leak it.
      if (disposedRef.current) return
      const socket = new WebSocket(url)
      socket.binaryType = 'arraybuffer'
      socketRef.current = socket

      socket.onopen = () => {
        retryRef.current = 0
        // Send the true geometry immediately: the query params were a guess
        // made before the first fit().
        socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }

      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(event.data))
          return
        }
        try {
          const message = JSON.parse(event.data as string)
          if (message.type === 'attached') setStatus('attached')
          if (message.type === 'error') {
            setStatus('error')
            term.writeln(`\r\n\x1b[31m[moonphase] ${message.message}\x1b[0m`)
          }
        } catch {
          term.write(event.data as string)
        }
      }

      socket.onclose = (event) => {
        if (disposedRef.current) return
        setStatus('disconnected')
        // 4xxx codes are our own deliberate refusals; retrying them would just
        // repeat the same error.
        if (event.code >= 4000 && event.code < 5000) {
          term.writeln(`\r\n\x1b[33m[moonphase] disconnected (${event.code})\x1b[0m`)
          return
        }
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000)
        retryRef.current += 1
        term.writeln(
          `\r\n\x1b[90m[moonphase] connection lost — reattaching in ${delay / 1000}s\x1b[0m`,
        )
        reconnectTimer = window.setTimeout(connect, delay)
      }

      socket.onerror = () => setStatus('error')
    }

    const onData = term.onData((data) => {
      if (readOnlyRef.current) {
        refusedRef.current?.()
        return
      }
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(new TextEncoder().encode(data))
      }
    })

    const onResize = term.onResize(({ cols, rows }) => {
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })

    // Fitting inside the observer callback resizes the element the observer
    // watches, which the browser reports as "loop completed with undelivered
    // notifications". Deferring a frame breaks that cycle.
    let fitFrame = 0
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(fitFrame)
      fitFrame = requestAnimationFrame(safeFit)
    })
    observer.observe(hostRef.current)

    void connect()

    return () => {
      disposedRef.current = true
      window.clearTimeout(reconnectTimer)
      cancelAnimationFrame(fitFrame)
      observer.disconnect()
      onData.dispose()
      onResize.dispose()
      socketRef.current?.close()

      // Dispose on the next macrotask rather than inline. xterm's Viewport
      // queues a zero-delay timer for syncScrollArea; disposing synchronously
      // tears down the render service before that callback runs, and it then
      // throws reading `dimensions` from undefined. Timers fire FIFO, so
      // scheduling ours last lets xterm's own callback complete first.
      // React's StrictMode double-mount makes this reproducible, but any fast
      // unmount — switching projects quickly — hits the same race.
      window.setTimeout(() => term.dispose(), 0)
      termRef.current = null
    }
  }, [projectId, session])

  return (
    <div className={`terminal-wrap${readOnly ? ' read-only' : ''}`}>
      {readOnly && (
        <div className="terminal-readonly" aria-hidden="true">
          read-only
        </div>
      )}
      <div className={`terminal-status terminal-status--${status}`}>
        <span className="dot" />
        {status === 'attached'
          ? 'attached'
          : status === 'connecting'
            ? 'attaching…'
            : status === 'disconnected'
              ? 'detached'
              : 'error'}
      </div>
      <div ref={hostRef} className="terminal-host" />
    </div>
  )
}
