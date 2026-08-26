import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { terminalUrl } from '../lib/api'
import { copyText } from '../lib/clipboard'

type Status = 'connecting' | 'attached' | 'disconnected' | 'error'

/**
 * xterm hands Enter and Shift+Enter to the terminal identically unless we
 * intervene, so `attachCustomKeyEventHandler` checks for this before letting
 * either through to the default handling.
 */
export function isShiftEnter(event: Pick<KeyboardEvent, 'key' | 'shiftKey'>): boolean {
  return event.key === 'Enter' && event.shiftKey
}

/**
 * Real terminals distinguish Shift+Enter from Enter by sending ESC before
 * the carriage return rather than a bare `\r` — VS Code's, iTerm2's, Zed's
 * and Alacritty's own Shift+Enter bindings all send exactly this, and it is
 * what the harness's keypress parser reads as "newline, don't submit".
 */
export const SHIFT_ENTER_SEQUENCE = '\x1b\r'

// A silent network drop (Wi-Fi association lost, a VPN blip, laptop sleep)
// often never fires `onclose`/`onerror` at all — the socket just sits
// half-open until some lengthy OS-level TCP timeout, if that ever comes. A
// heartbeat forces detection well before that.
const HEARTBEAT_INTERVAL_MS = 10_000

/** Re-encodes any pasted image as PNG — the one format the in-container xclip
 * shim (see infra/images/claude/xclip-shim.sh) knows how to hand back. */
async function imageBlobToPngBase64(blob: Blob): Promise<string> {
  const bitmap = await createImageBitmap(blob)
  const canvas = document.createElement('canvas')
  canvas.width = bitmap.width
  canvas.height = bitmap.height
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('2D canvas unavailable')
  ctx.drawImage(bitmap, 0, 0)
  bitmap.close()
  const dataUrl = canvas.toDataURL('image/png')
  return dataUrl.slice(dataUrl.indexOf(',') + 1)
}

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
    // Captured once: React can null the ref before this effect's cleanup
    // runs, but the DOM node it pointed to is still the one to unhook from.
    const host = hostRef.current
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

    /**
     * tmux's mouse mode (tmux.conf: `set -g mouse on`) is what makes a
     * click-drag highlight text at all: it owns the mouse, runs its own
     * copy-mode selection, and on release copies the highlight into its
     * buffer — which is why the highlight vanishes the moment you let go,
     * with or without this handler. By default tmux also reports that copy
     * back out via an OSC 52 escape sequence, so the browser is the only
     * side missing a handler for it. Without one, xterm.js just drops the
     * sequence and the text never reaches the system clipboard.
     */
    const clipboardHandler = term.parser.registerOscHandler(52, (data) => {
      const separator = data.indexOf(';')
      if (separator === -1) return true
      const targets = data.slice(0, separator)
      const payload = data.slice(separator + 1)
      // "?" is a request to read the clipboard back into the pane. Silently
      // declined: browsers gate reads behind a user gesture we don't have
      // here, and honouring reads at all would let anything running in the
      // session sniff the clipboard on a whim.
      if (payload === '?' || !targets.includes('c')) return true
      try {
        const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0))
        void copyText(new TextDecoder().decode(bytes))
      } catch {
        // Malformed payload — nothing sane to copy.
      }
      return true
    })

    term.open(host)

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
    let heartbeatTimer: number | undefined
    // Set when a ping has gone unanswered — the socket looks OPEN but the
    // peer has stopped responding, which is exactly the silent-drop case a
    // plain onclose/onerror listener misses.
    let awaitingPong = false

    const stopHeartbeat = () => {
      window.clearInterval(heartbeatTimer)
      heartbeatTimer = undefined
      awaitingPong = false
    }

    const startHeartbeat = (socket: WebSocket) => {
      stopHeartbeat()
      heartbeatTimer = window.setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return
        if (awaitingPong) {
          // No pong since the last ping: force the close/reconnect path now
          // rather than waiting on an OS-level TCP timeout that may never
          // fire on its own.
          socket.close()
          return
        }
        awaitingPong = true
        socket.send(JSON.stringify({ type: 'ping' }))
      }, HEARTBEAT_INTERVAL_MS)
    }

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
        startHeartbeat(socket)
      }

      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(event.data))
          return
        }
        try {
          const message = JSON.parse(event.data as string)
          if (message.type === 'attached') setStatus('attached')
          if (message.type === 'pong') awaitingPong = false
          if (message.type === 'error') {
            setStatus('error')
            term.writeln(`\r\n\x1b[31m[moonphase] ${message.message}\x1b[0m`)
          }
        } catch {
          term.write(event.data as string)
        }
      }

      socket.onclose = (event) => {
        stopHeartbeat()
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

    // The browser doesn't reliably fire onclose/onerror on a silent drop —
    // Wi-Fi association lost, a VPN blip, a laptop resuming from sleep — so
    // the socket can sit half-open indefinitely. When the OS tells us
    // connectivity is back (or the tab becomes visible again, which covers
    // the sleep/wake case even when 'online' doesn't fire), probe the
    // existing socket immediately instead of waiting for the next heartbeat
    // tick or a backed-off reconnect that was scheduled before the outage.
    const checkLiveness = () => {
      if (disposedRef.current) return
      const socket = socketRef.current
      if (socket && socket.readyState === WebSocket.OPEN) {
        if (awaitingPong) {
          socket.close()
        } else {
          awaitingPong = true
          socket.send(JSON.stringify({ type: 'ping' }))
        }
        return
      }
      if (
        reconnectTimer !== undefined &&
        (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING)
      ) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = undefined
        retryRef.current = 0
        void connect()
      }
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') checkLiveness()
    }
    window.addEventListener('online', checkLiveness)
    document.addEventListener('visibilitychange', onVisible)

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

    // xterm treats Enter and Shift+Enter identically — both would otherwise
    // submit the line below. Intercepting here, ahead of that default
    // handling, is what lets Shift+Enter send a distinguishable sequence
    // instead (see SHIFT_ENTER_SEQUENCE for what the harness expects).
    const onKeyEvent = (event: KeyboardEvent) => {
      if (event.type !== 'keydown' || !isShiftEnter(event)) return true
      if (readOnlyRef.current) {
        refusedRef.current?.()
        return false
      }
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(new TextEncoder().encode(SHIFT_ENTER_SEQUENCE))
      }
      return false
    }
    term.attachCustomKeyEventHandler(onKeyEvent)

    /**
     * The harness's own image paste shells out to the OS clipboard, which
     * this container does not have — see xclip-shim.sh for the other half of
     * this. Only image data changes anything here: a plain text paste is
     * left alone and reaches xterm's own paste handling exactly as before.
     *
     * Ordering matters. The staged image must land in the container before
     * the paste that makes the harness go looking for it, so the default
     * (synchronous) paste is suppressed and replayed manually through
     * `term.paste()` once staging is confirmed sent — both then go out over
     * the one WebSocket in that order, and the server's single-threaded
     * receive loop (see terminal.py) preserves it the rest of the way.
     */
    const onPaste = (event: ClipboardEvent) => {
      if (readOnlyRef.current) {
        event.preventDefault()
        event.stopImmediatePropagation()
        refusedRef.current?.()
        return
      }
      const items = event.clipboardData?.items
      const imageItem = items && Array.from(items).find((item) => item.type.startsWith('image/'))
      if (!imageItem) return

      const file = imageItem.getAsFile()
      const fallbackText = event.clipboardData?.getData('text/plain') ?? ''
      event.preventDefault()
      event.stopImmediatePropagation()
      if (!file) return

      void (async () => {
        try {
          const base64 = await imageBlobToPngBase64(file)
          const socket = socketRef.current
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: 'clipboard-image', data: base64 }))
          }
        } catch {
          // Nothing sane to stage — the harness still gets whatever paste
          // would otherwise have happened, below.
        } finally {
          term.paste(fallbackText)
        }
      })()
    }
    host.addEventListener('paste', onPaste, { capture: true })

    // Fitting inside the observer callback resizes the element the observer
    // watches, which the browser reports as "loop completed with undelivered
    // notifications". Deferring a frame breaks that cycle.
    let fitFrame = 0
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(fitFrame)
      fitFrame = requestAnimationFrame(safeFit)
    })
    observer.observe(host)

    void connect()

    return () => {
      disposedRef.current = true
      window.clearTimeout(reconnectTimer)
      stopHeartbeat()
      window.removeEventListener('online', checkLiveness)
      document.removeEventListener('visibilitychange', onVisible)
      cancelAnimationFrame(fitFrame)
      observer.disconnect()
      host.removeEventListener('paste', onPaste, { capture: true })
      onData.dispose()
      onResize.dispose()
      clipboardHandler.dispose()
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
    // `readOnly` is deliberately absent: it is read through a ref so that a
    // change in access does not tear down and rebuild the terminal, which
    // would drop the connection and the scrollback with it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
