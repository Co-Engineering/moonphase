import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { terminalUrl, uploadSessionFile } from '../lib/api'
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

/**
 * What the harness's own "check the clipboard for an image" code is bound
 * to, and the only thing that reaches it — a bracketed-paste of text (even
 * empty text) is a different code path entirely and never triggers it. See
 * the onPaste handler below for how staging and this are sequenced.
 */
export const HARNESS_CLIPBOARD_PASTE_TRIGGER = '\x16'

/**
 * Plain Ctrl+V — the paste gesture the browser never tells us about.
 *
 * xterm's own key handling turns Ctrl+letter into the matching control
 * character and cancels the event (`evaluateKeyboardEvent`, the
 * `ev.ctrlKey && !ev.shiftKey && !ev.altKey && !ev.metaKey` branch), so
 * Ctrl+V arrives at the harness as 0x16 — the same byte as
 * HARNESS_CLIPBOARD_PASTE_TRIGGER above — and no `paste` event is ever
 * fired. That is the gap: an image on the clipboard only reached the
 * harness through a right-click paste, Ctrl+Shift+V, or a drop, never
 * through the most obvious gesture of all.
 *
 * Cmd+V is deliberately NOT included, and the distinction is not cosmetic.
 * xterm claims only Cmd+A on macOS; every other Cmd chord falls through
 * `if (!result.key) return true`, uncancelled, so the browser goes on to
 * fire its own `paste` event — which the onPaste handler below already
 * turns into a staged image. Intercepting Cmd+V here would call
 * preventDefault on the one combo that does work, suppressing that event:
 * an image would still arrive, but pasted *text* would stop arriving
 * entirely and land as a stray 0x16 instead. Verified against xterm's
 * sources rather than assumed, after a real Ctrl+V in a real browser was
 * observed producing `onData 0x16` and no paste event at all.
 */
export function isPlainPasteCombo(
  event: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'altKey'>,
): boolean {
  return (
    event.key.toLowerCase() === 'v' &&
    event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey &&
    !event.altKey
  )
}

/**
 * Wraps a filename for safe insertion into a shell command line: single
 * quotes, with any embedded single quote escaped the POSIX way (close the
 * quote, an escaped literal quote, reopen it). Used when an uploaded file's
 * name is pasted into the terminal after landing, so a name with a space or
 * shell-special character still reads as the one argument it is.
 */
export function shellQuoteForPaste(name: string): string {
  return `'${name.replace(/'/g, `'\\''`)}'`
}

/**
 * How long to wait on the browser for a clipboard image before giving up and
 * letting the keystroke through as itself.
 *
 * `navigator.clipboard.read()` needs the `clipboard-read` permission, and
 * until the viewer answers that prompt the promise simply stays pending —
 * it does not reject. Awaiting it unbounded means Ctrl+V does *nothing at
 * all* while a prompt the viewer may never have noticed sits open, because
 * the keystroke was already swallowed by preventDefault. Half a second is
 * far longer than a granted read takes and far shorter than a person takes
 * to answer a prompt.
 */
export const CLIPBOARD_READ_TIMEOUT_MS = 500

/**
 * The clipboard's image, or null if there isn't one, we aren't allowed to
 * look, or looking is taking long enough that it must be waiting on a
 * person. Never rejects: every one of those is the same answer here — carry
 * on and let Ctrl+V mean what it has always meant.
 */
export async function readClipboardImage(
  clipboard: Clipboard | undefined = navigator.clipboard,
  timeoutMs: number = CLIPBOARD_READ_TIMEOUT_MS,
): Promise<Blob | null> {
  if (!clipboard?.read) return null
  const read = (async () => {
    try {
      for (const item of await clipboard.read()) {
        const type = item.types.find((t) => t.startsWith('image/'))
        if (type) return await item.getType(type)
      }
    } catch {
      // Refused, unsupported, or nothing readable this way.
    }
    return null
  })()
  return Promise.race([
    read,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs)),
  ])
}

/**
 * What to do once a copy of the pasted image has (or hasn't) made it onto
 * the harness's side, split out from the paste handler because it has no
 * dependency on xterm/canvas/the socket and so can be tested directly rather
 * than through a canvas encode this environment cannot run.
 */
export function clipboardImagePasteFollowUp(
  staged: boolean,
  fallbackText: string,
): { pasteText: string | null; sendTrigger: boolean } {
  return { pasteText: fallbackText || null, sendTrigger: staged }
}

type KeydownLike = Pick<KeyboardEvent, 'key' | 'shiftKey' | 'type' | 'preventDefault' | 'stopPropagation'>

/**
 * The xterm custom key handler for Shift+Enter.
 *
 * Returning `false` from `attachCustomKeyEventHandler` only tells xterm to
 * skip its own key handling — unlike that handling, it does not call
 * `preventDefault`. Left alone, the browser's native default action for
 * Enter in the textarea xterm renders into (inserting a literal newline)
 * still fires right behind whatever this sends, so `preventDefault` and
 * `stopPropagation` are called explicitly, every time, before anything else.
 */
export function handleShiftEnterKeydown(
  event: KeydownLike,
  { readOnly, onRefused, send }: { readOnly: boolean; onRefused?: () => void; send: (bytes: Uint8Array) => void },
): boolean {
  if (event.type !== 'keydown' || !isShiftEnter(event)) return true
  event.preventDefault()
  event.stopPropagation()
  if (readOnly) {
    onRefused?.()
    return false
  }
  send(new TextEncoder().encode(SHIFT_ENTER_SEQUENCE))
  return false
}

// A silent network drop (Wi-Fi association lost, a VPN blip, laptop sleep)
// often never fires `onclose`/`onerror` at all — the socket just sits
// half-open until some lengthy OS-level TCP timeout, if that ever comes. A
// heartbeat forces detection well before that.
const HEARTBEAT_INTERVAL_MS = 10_000

/**
 * Decodes an OSC 52 clipboard-set payload into the text it names, or null if
 * there is nothing sane to copy: a read request (`?`, always declined — see
 * the handler below), a target that is not the clipboard, or bytes that do
 * not decode as base64/UTF-8.
 */
export function decodeOsc52ClipboardPayload(data: string): string | null {
  const separator = data.indexOf(';')
  if (separator === -1) return null
  const targets = data.slice(0, separator)
  const payload = data.slice(separator + 1)
  if (payload === '?') return null
  // tmux's own copy (mouse-drag release, not a passthrough of some inner
  // program's own OSC 52) was captured live sending an empty Pc field —
  // "52;;<base64>", not "52;c;<base64>" — so an empty target has to mean
  // "the clipboard" too, or every ordinary tmux copy is silently discarded
  // right here regardless of anything else. Only an explicit *other*
  // target (primary selection "p", a cut buffer) should be turned away.
  if (targets && !targets.includes('c')) return null
  try {
    const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return null
  }
}

/**
 * How long a tmux copy waits, unclaimed, for the next real interaction with
 * this terminal before it is treated as abandoned rather than flushed to the
 * system clipboard on some much-later keystroke that has nothing to do with
 * it.
 */
export const PENDING_CLIPBOARD_TIMEOUT_MS = 15_000

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
  // A tmux copy that arrived over OSC 52 but hasn't yet ridden out on a real
  // user gesture — see the effect below for why it can't be written to the
  // clipboard the moment it arrives.
  const pendingClipboardRef = useRef<string | null>(null)
  const pendingClipboardTimeoutRef = useRef<number | undefined>(undefined)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const uploadTimerRef = useRef<number | undefined>(undefined)

  const [status, setStatus] = useState<Status>('connecting')
  // Set briefly when a keystroke had nowhere to go — the socket wasn't open,
  // for any reason short of the deliberate read-only refusal above. Without
  // this, typing during a reconnect (the exact gap the heartbeat and backoff
  // below exist to survive) just vanishes with no sign it never arrived.
  const [inputDropped, setInputDropped] = useState(false)
  // True for the stretch between a pasted image being detected and the
  // staging message actually going out — a canvas re-encode plus an SSH
  // write on the other end of the socket, easily the better part of a second
  // for a large screenshot, with nothing on screen to say it's in progress.
  const [pastingImage, setPastingImage] = useState(false)
  // Mirrors pastingImage for a dropped or picked file that isn't an image:
  // there is no clipboard-paste convention to piggyback on here, so this is
  // the only sign an upload is in flight, succeeded, or failed.
  const [uploadState, setUploadState] = useState<
    { kind: 'uploading' | 'done' | 'error'; label: string } | null
  >(null)

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

  // Independent of the connection effect below (which tears down and rebuilds
  // the whole terminal on a project/session change): only the pending-timer
  // needs cleaning up on unmount, not on every reattach.
  useEffect(() => () => window.clearTimeout(uploadTimerRef.current), [])

  /**
   * Uploads each file into the session's working directory, then pastes the
   * name(s) it landed under at the cursor — the same convention dragging a
   * file onto Terminal.app or iTerm2 follows, typing its path rather than
   * silently doing something with it. Shared by the drop handler below and
   * the explicit upload button, so both end up in the same place.
   */
  const uploadFiles = (files: File[]) => {
    if (readOnlyRef.current || files.length === 0) return
    void (async () => {
      setUploadState({
        kind: 'uploading',
        label: files.length === 1 ? files[0].name : `${files.length} files`,
      })
      const landed: string[] = []
      let failed = 0
      for (const file of files) {
        try {
          const { path } = await uploadSessionFile(projectId, file, session)
          landed.push(path)
        } catch {
          failed += 1
        }
      }
      if (landed.length) {
        termRef.current?.paste(landed.map(shellQuoteForPaste).join(' '))
      }
      setUploadState(
        failed > 0
          ? {
              kind: 'error',
              label: landed.length ? `${failed} of ${files.length} failed` : 'upload failed',
            }
          : { kind: 'done', label: landed.length === 1 ? landed[0] : `${landed.length} files` },
      )
      window.clearTimeout(uploadTimerRef.current)
      uploadTimerRef.current = window.setTimeout(() => setUploadState(null), 2500)
    })()
  }

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
     *
     * It cannot just call the clipboard API here, though. This sequence
     * only exists at all because it travelled mouseup → this socket → SSH →
     * tmux → SSH → this socket again, and every one of those hops is time
     * a browser's "was this actually asked for by the person sitting here"
     * check does not forgive: Chromium refuses a write once transient
     * activation is more than about a second stale (and separately, once
     * the document has lost focus in the meantime), Firefox and Safari
     * refuse outright unless the write happens inside a real gesture's own
     * handler. A real round trip is essentially always slower than that
     * window. So the payload is buffered here and only actually written to
     * the clipboard from inside the next genuine mousedown/mouseup/keydown
     * on this terminal (below) — a real gesture, just not the one that
     * produced this particular text.
     */
    const clipboardHandler = term.parser.registerOscHandler(52, (data) => {
      // "?" is a request to read the clipboard back into the pane, already
      // filtered out by decodeOsc52ClipboardPayload below. Silently
      // declined regardless: browsers gate reads behind a user gesture we
      // don't have here, and honouring reads at all would let anything
      // running in the session sniff the clipboard on a whim.
      const text = decodeOsc52ClipboardPayload(data)
      if (text) {
        pendingClipboardRef.current = text
        window.clearTimeout(pendingClipboardTimeoutRef.current)
        pendingClipboardTimeoutRef.current = window.setTimeout(() => {
          pendingClipboardRef.current = null
        }, PENDING_CLIPBOARD_TIMEOUT_MS)
      }
      return true
    })

    // The gesture that actually earns the write. Deliberately three event
    // types rather than one: whichever the person does next — clicking to
    // start another selection, releasing it, or simply typing on — should
    // flush a copy that's still waiting, not just one specific key.
    const flushPendingClipboard = () => {
      const text = pendingClipboardRef.current
      if (!text) return
      pendingClipboardRef.current = null
      window.clearTimeout(pendingClipboardTimeoutRef.current)
      void copyText(text)
    }
    host.addEventListener('mousedown', flushPendingClipboard)
    host.addEventListener('mouseup', flushPendingClipboard)
    host.addEventListener('keydown', flushPendingClipboard)

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
    let dropTimer: number | undefined
    // Set when a ping has gone unanswered — the socket looks OPEN but the
    // peer has stopped responding, which is exactly the silent-drop case a
    // plain onclose/onerror listener misses.
    let awaitingPong = false

    const flashDropped = () => {
      setInputDropped(true)
      window.clearTimeout(dropTimer)
      dropTimer = window.setTimeout(() => setInputDropped(false), 900)
    }

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
      // A handshake the server refused and a connection that dropped both
      // arrive here as a bare 1006 with no reason attached — the refusal
      // happens before the socket is accepted, so there is nowhere to put
      // one. This is the only thing that tells them apart.
      let everOpened = false

      socket.onopen = () => {
        everOpened = true
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
          // The paste itself worked and the connection is fine — only the
          // image didn't land — so this says so without touching `status`,
          // which would otherwise misreport a healthy connection as broken.
          if (message.type === 'clipboard-image-error') {
            term.writeln(`\r\n\x1b[33m[moonphase] ${message.message}\x1b[0m`)
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
          everOpened
            ? `\r\n\x1b[90m[moonphase] connection lost — reattaching in ${delay / 1000}s\x1b[0m`
            : // Never opened, so nothing was lost: the server answered the
              // handshake with a refusal. Retrying is still right — this is
              // also what a restarting server looks like — but calling it a
              // lost connection sends you looking at your network, and
              // v0.9.0's Origin regression sat behind exactly that sentence
              // with nothing on screen to contradict it.
              `\r\n\x1b[33m[moonphase] the server refused this connection` +
              ` (it never opened) — retrying in ${delay / 1000}s\x1b[0m`,
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
      } else {
        flashDropped()
      }
    })

    const onResize = term.onResize(({ cols, rows }) => {
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })

    /**
     * The harness's own image paste shells out to the OS clipboard, which
     * this container does not have — see xclip-shim.sh for the other half of
     * this. Only image data changes anything here: a plain text paste is
     * left alone and reaches xterm's own paste handling exactly as before.
     *
     * Ordering matters. The staged image must land in the container before
     * the harness goes looking for it, so the default (synchronous) paste is
     * suppressed and the trigger is sent manually once staging is confirmed
     * sent — both then go out over the one WebSocket in that order, and the
     * server's single-threaded receive loop (see terminal.py) preserves it
     * the rest of the way.
     *
     * The trigger is Ctrl+V itself (0x16), not a text paste: the harness's
     * own "check the clipboard for an image" code is bound to that keystroke
     * specifically and nothing else reaches it — `term.paste()` delivers
     * bracketed-paste text, which is a different thing even when the text
     * happens to be empty, so it was never going to ask the harness to look.
     *
     * Shared with drag-and-drop below, and with the explicit Ctrl+V handling
     * further down: a dropped file, and an image read directly off the
     * clipboard, both need exactly this same staging trip.
     */
    const stageImage = (file: Blob, fallbackText: string) => {
      if (socketRef.current?.readyState !== WebSocket.OPEN) {
        // Nothing to encode toward — say so now rather than spending a
        // moment re-encoding a screenshot only to find there was nowhere to
        // send it.
        flashDropped()
        if (fallbackText) term.paste(fallbackText)
        return
      }

      setPastingImage(true)
      void (async () => {
        let staged = false
        try {
          const base64 = await imageBlobToPngBase64(file)
          const socket = socketRef.current
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: 'clipboard-image', data: base64 }))
            staged = true
          } else {
            flashDropped()
          }
        } catch {
          // Nothing sane to stage — the harness still gets whatever paste
          // would otherwise have happened, below.
        } finally {
          const { pasteText, sendTrigger } = clipboardImagePasteFollowUp(staged, fallbackText)
          if (pasteText) term.paste(pasteText)
          if (sendTrigger) {
            const socket = socketRef.current
            if (socket?.readyState === WebSocket.OPEN) {
              socket.send(new TextEncoder().encode(HARNESS_CLIPBOARD_PASTE_TRIGGER))
            }
          }
          setPastingImage(false)
        }
      })()
    }

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

      stageImage(file, fallbackText)
    }
    host.addEventListener('paste', onPaste, { capture: true })

    // Dropping a file is claimed here rather than left to the browser's
    // default (which, over a bare div, is to navigate to it). An image goes
    // to the same clipboard-staging destination a paste would; anything else
    // is uploaded into the session's working directory instead — see
    // uploadFiles above.
    const onDragOver = (event: DragEvent) => {
      if (!readOnlyRef.current) event.preventDefault()
    }
    const onDrop = (event: DragEvent) => {
      event.preventDefault()
      if (readOnlyRef.current) {
        refusedRef.current?.()
        return
      }
      const dropped = Array.from(event.dataTransfer?.files ?? [])
      const image = dropped.find((f) => f.type.startsWith('image/'))
      if (image) stageImage(image, '')
      const rest = dropped.filter((f) => f !== image)
      if (rest.length) uploadFiles(rest)
    }
    host.addEventListener('dragover', onDragOver)
    host.addEventListener('drop', onDrop)

    // xterm treats Enter and Shift+Enter identically — both would otherwise
    // submit the line below. Intercepting here, ahead of that default
    // handling, is what lets Shift+Enter send a distinguishable sequence
    // instead (see handleShiftEnterKeydown for what the harness expects, and
    // why this cannot just return `false` and stop there).
    //
    // Ctrl+V is intercepted for the same reason `onPaste` exists at all:
    // nothing else asks the browser to read an image off the clipboard for
    // this specific, otherwise-unremarkable keystroke (see
    // isPlainPasteCombo for why, and for why Cmd+V is left alone — the
    // browser does fire a paste event for that one, and onPaste below
    // already handles it). Reading the clipboard here directly, rather than
    // waiting on a paste event this combo never generates, is what makes
    // the most obvious way to paste an image actually work, rather than
    // only a right-click paste or a drop.
    term.attachCustomKeyEventHandler((event) => {
      const shiftEnterResult = handleShiftEnterKeydown(event, {
        readOnly: readOnlyRef.current,
        onRefused: refusedRef.current,
        send: (bytes) => {
          const socket = socketRef.current
          if (socket?.readyState === WebSocket.OPEN) socket.send(bytes)
          else flashDropped()
        },
      })
      if (shiftEnterResult === false) return false
      if (event.type !== 'keydown' || !isPlainPasteCombo(event)) return true

      event.preventDefault()
      event.stopPropagation()
      if (readOnlyRef.current) {
        refusedRef.current?.()
        return false
      }

      const sendTrigger = () => {
        const socket = socketRef.current
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(new TextEncoder().encode(HARNESS_CLIPBOARD_PASTE_TRIGGER))
        } else {
          flashDropped()
        }
      }

      void (async () => {
        const image = await readClipboardImage()
        if (image) stageImage(image, '')
        // Whether or not an image turned up, the harness still has to be
        // told to look — staging alone puts the file where xclip-shim will
        // find it, and this keystroke is what sends it looking.
        else sendTrigger()
      })()
      return false
    })

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
      window.clearTimeout(dropTimer)
      stopHeartbeat()
      window.removeEventListener('online', checkLiveness)
      document.removeEventListener('visibilitychange', onVisible)
      cancelAnimationFrame(fitFrame)
      observer.disconnect()
      host.removeEventListener('paste', onPaste, { capture: true })
      host.removeEventListener('dragover', onDragOver)
      host.removeEventListener('drop', onDrop)
      host.removeEventListener('mousedown', flushPendingClipboard)
      host.removeEventListener('mouseup', flushPendingClipboard)
      host.removeEventListener('keydown', flushPendingClipboard)
      window.clearTimeout(pendingClipboardTimeoutRef.current)
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
      <div className="terminal-toolbar">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="terminal-upload-input"
          tabIndex={-1}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? [])
            event.target.value = ''
            if (files.length) uploadFiles(files)
          }}
        />
        <button
          type="button"
          className="terminal-upload-button"
          title={
            readOnly
              ? 'This session belongs to someone else'
              : 'Upload a file into this session'
          }
          aria-label="Upload a file into this session"
          onClick={() => (readOnly ? onRefusedInput?.() : fileInputRef.current?.click())}
        >
          +
        </button>
        <div
          className={`terminal-status terminal-status--${status}${
            inputDropped ? ' input-dropped' : ''
          }${uploadState?.kind === 'error' ? ' upload-error' : ''}`}
        >
          <span className="dot" />
          {uploadState
            ? uploadState.kind === 'uploading'
              ? `uploading ${uploadState.label}…`
              : uploadState.kind === 'done'
                ? `uploaded ${uploadState.label}`
                : uploadState.label
            : pastingImage
              ? 'pasting image…'
              : status === 'attached'
                ? 'attached'
                : status === 'connecting'
                  ? 'attaching…'
                  : status === 'disconnected'
                    ? 'detached'
                    : 'error'}
        </div>
      </div>
      <div ref={hostRef} className="terminal-host" />
    </div>
  )
}
