import { useCallback, useEffect, useRef, useState } from 'react'
import { api, feedUrl, type DiffLine, type FeedEvent, type Prompt } from '../lib/api'

// Mirrors the backend's own limit (feed.py's _MAX_UPLOAD_BYTES) so an
// oversized file is refused here — instantly, before it ever leaves the
// device — rather than after however long a phone upload takes to fail.
const MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
const MAX_ATTACHMENT_MESSAGE = 'Images are limited to 15 MB.'

interface Attachment {
  id: string
  file: File
  previewUrl: string
  /** Set once the upload lands and the container has a path to point at. */
  path: string | null
  uploading: boolean
  error: string | null
}

interface Props {
  projectId: string
  session: string
  running: boolean
  /**
   * Shared with view-only access. The feed is the whole point of a read-only
   * share, so it streams exactly as it does for anyone else; only the ways of
   * putting something *into* the session go away.
   */
  readOnly?: boolean
  /** Called when an attempt to send was refused, so the caller can react. */
  onRefusedInput?: () => void
}

const TOOL_ICON: Record<string, string> = {
  Read: '◇',
  Edit: '✎',
  Write: '✎',
  Bash: '$',
  Grep: '⌕',
  Glob: '⌕',
  Task: '⚙',
  WebFetch: '↓',
  WebSearch: '⌕',
}

/** Newest wins on id, and the buffer is bounded — a long session is unbounded. */
const MAX_EVENTS = 600

function merge(current: FeedEvent[], incoming: FeedEvent[]): FeedEvent[] {
  if (incoming.length === 0) return current
  const seen = new Set(current.map((e) => e.id))
  const fresh = incoming.filter((e) => !seen.has(e.id))
  return fresh.length ? [...current, ...fresh].slice(-MAX_EVENTS) : current
}

/**
 * The phone client.
 *
 * A readable account of what the agent is doing, rather than an 80-column TUI
 * on a 390-pixel screen. It never attaches a terminal — partly because it does
 * not need one, and partly because tmux sizes a window to its most recent
 * client, so a phone attaching would squeeze the desktop down to phone width.
 *
 * Everything it sends goes through the same session the desktop is attached
 * to, so answering here shows up there as if it had been typed.
 */
export function Feed({
  projectId,
  session,
  running,
  readOnly = false,
  onRefusedInput,
}: Props) {
  const [events, setEvents] = useState<FeedEvent[]>([])
  const [prompt, setPrompt] = useState<Prompt | null>(null)
  const [activity, setActivity] = useState('unknown')
  const [available, setAvailable] = useState(true)
  const [live, setLive] = useState(false)
  // Briefly true right after falling back to polling, so the change is
  // visible rather than only ever knowable from a hover title — which is
  // never available on the phone this view is mainly built for.
  const [justWentQuiet, setJustWentQuiet] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [attachments, setAttachments] = useState<Attachment[]>([])

  const bottomRef = useRef<HTMLDivElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const messageRef = useRef<HTMLTextAreaElement | null>(null)
  // Only follow new output when the reader is already at the bottom; yanking
  // the view while someone is reading history is worse than a missed update.
  const pinnedRef = useRef(true)
  const socketRef = useRef<WebSocket | null>(null)
  const disposedRef = useRef(false)

  // Object URLs are the browser's, not React's — they leak until revoked, so
  // every attachment that ever existed gets cleaned up on the way out.
  const revokeAttachments = useCallback((list: Attachment[]) => {
    for (const a of list) URL.revokeObjectURL(a.previewUrl)
  }, [])
  const attachmentsRef = useRef<Attachment[]>([])
  useEffect(() => {
    attachmentsRef.current = attachments
  }, [attachments])
  useEffect(() => () => revokeAttachments(attachmentsRef.current), [revokeAttachments])

  const addFiles = useCallback(
    (files: Iterable<File>) => {
      const images = Array.from(files).filter((f) => f.type.startsWith('image/'))
      if (!images.length) return
      const next: Attachment[] = images.map((file) => {
        const tooLarge = file.size > MAX_ATTACHMENT_BYTES
        return {
          id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
          file,
          previewUrl: URL.createObjectURL(file),
          path: null,
          uploading: !tooLarge,
          error: tooLarge ? MAX_ATTACHMENT_MESSAGE : null,
        }
      })
      setAttachments((current) => [...current, ...next])
      for (const attachment of next) {
        if (attachment.error) continue // too large — nothing to upload
        api
          .uploadFeedImage(projectId, attachment.file, session)
          .then((res) =>
            setAttachments((current) =>
              current.map((a) =>
                a.id === attachment.id ? { ...a, uploading: false, path: res.path } : a,
              ),
            ),
          )
          .catch((err) =>
            setAttachments((current) =>
              current.map((a) =>
                a.id === attachment.id
                  ? {
                      ...a,
                      uploading: false,
                      error: err instanceof Error ? err.message : String(err),
                    }
                  : a,
              ),
            ),
          )
      }
    },
    [projectId, session],
  )

  const removeAttachment = useCallback(
    (id: string) => {
      setAttachments((current) => {
        const found = current.find((a) => a.id === id)
        if (found) revokeAttachments([found])
        return current.filter((a) => a.id !== id)
      })
    },
    [revokeAttachments],
  )

  useEffect(() => {
    if (!running) return
    disposedRef.current = false
    setEvents([])
    setPrompt(null)
    setAttachments((current) => {
      revokeAttachments(current)
      return []
    })
    pinnedRef.current = true

    let pollTimer: number | undefined
    let reconnectTimer: number | undefined
    let quietTimer: number | undefined
    let attempts = 0
    let cursor = ''

    /**
     * Fallback for when a socket cannot be held open — a proxy that strips
     * upgrades, or a flaky mobile connection. Slower, but the feed still works
     * rather than sitting empty.
     */
    const startPolling = () => {
      setLive(false)
      setJustWentQuiet(true)
      window.clearTimeout(quietTimer)
      quietTimer = window.setTimeout(() => setJustWentQuiet(false), 4000)
      const tick = async () => {
        if (disposedRef.current) return
        try {
          const page = await api.feed(projectId, session, cursor || undefined)
          cursor = page.cursor
          setAvailable(page.available)
          setActivity(page.activity)
          setPrompt(page.prompt)
          setEvents((current) => merge(current, page.events))
          setError(null)
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err))
        }
        pollTimer = window.setTimeout(tick, 3000)
      }
      void tick()
    }

    const connect = async () => {
      if (disposedRef.current) return
      let socket: WebSocket
      try {
        socket = new WebSocket(await feedUrl(projectId, session))
      } catch {
        startPolling()
        return
      }
      if (disposedRef.current) {
        socket.close()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        attempts = 0
        setLive(true)
        setError(null)
      }

      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data as string)
          if (msg.type === 'page') {
            setAvailable(msg.available ?? true)
            setEvents(msg.events ?? [])
          } else if (msg.type === 'events') {
            setEvents((current) => merge(current, msg.events ?? []))
          } else if (msg.type === 'prompt') {
            setPrompt(msg.prompt ?? null)
            if (msg.activity) setActivity(msg.activity)
          } else if (msg.type === 'error') {
            setError(msg.message)
          }
        } catch {
          // A malformed frame is not worth tearing the feed down for.
        }
      }

      socket.onclose = (closed) => {
        setLive(false)
        if (disposedRef.current) return
        // 4xxx are our own refusals; retrying repeats the same answer.
        if (closed.code >= 4000 && closed.code < 5000) {
          if (closed.code === 4409) setAvailable(false)
          return
        }
        attempts += 1
        // After a couple of failures the socket is probably not going to
        // work here at all; polling is better than an empty screen.
        if (attempts >= 3) {
          startPolling()
          return
        }
        reconnectTimer = window.setTimeout(connect, 1000 * attempts)
      }
    }

    void connect()

    return () => {
      disposedRef.current = true
      window.clearTimeout(pollTimer)
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(quietTimer)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [projectId, session, running, revokeAttachments])

  useEffect(() => {
    if (pinnedRef.current) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [events, prompt])

  const onScroll = () => {
    const el = scrollerRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // Grows with what's typed rather than scrolling internally at one line —
  // resetting to 'auto' first is what lets it shrink back down again, since
  // scrollHeight only ever reports a height at least as tall as the current one.
  useEffect(() => {
    const el = messageRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [message])

  const send = useCallback(
    async (text: string, withAttachments: Attachment[] = []) => {
      const paths = withAttachments.filter((a) => a.path).map((a) => a.path as string)
      if (!text.trim() && paths.length === 0) return
      // A path per line ahead of the message, same as pasting one in by hand —
      // the harness reads it with its own Read tool, no special syntax needed.
      const body = [...paths, text.trim()].filter(Boolean).join('\n')
      setSending(true)
      setError(null)
      try {
        await api.answerFeed(projectId, body, session)
        setMessage('')
        if (withAttachments.length) {
          revokeAttachments(withAttachments)
          setAttachments([])
        }
        // The stream will report the result; clearing the prompt immediately
        // stops a tapped button sitting there looking unresponsive.
        setPrompt(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setSending(false)
      }
    },
    [projectId, session, revokeAttachments],
  )

  // An edit awaiting approval: show its diff with the question, so the answer
  // is made on the change rather than on a file name.
  const pendingDiff = prompt
    ? [...events].reverse().find((e) => e.kind === 'tool' && e.diff?.length)
    : undefined

  return (
    <div className="feed">
      <div className="feed-scroll" ref={scrollerRef} onScroll={onScroll}>
        {!running ? (
          <div className="empty">
            <h3>Project is not running</h3>
            Start it to see what the agent is doing.
          </div>
        ) : !available ? (
          <div className="empty">
            <h3>Nothing yet</h3>
            The agent has not written anything to this session.
          </div>
        ) : events.length === 0 ? (
          <div className="empty">Waiting for the first message…</div>
        ) : (
          events.map((event) => <FeedRow key={event.id} event={event} />)
        )}
        <div ref={bottomRef} />
      </div>

      {error && <div className="feed-error">{error}</div>}

      {prompt && (
        <div className="feed-prompt">
          {pendingDiff && (
            <Diff
              lines={pendingDiff.diff ?? []}
              added={pendingDiff.added}
              removed={pendingDiff.removed}
              truncated={pendingDiff.truncated}
              path={pendingDiff.text}
              startOpen
            />
          )}
          <div className="feed-question">{prompt.question}</div>
          <div className="feed-options">
            {prompt.options.map((option) => (
              <button
                key={option.key}
                className={option.key === '1' ? 'primary' : ''}
                disabled={sending}
                title={
                  readOnly
                    ? "This session belongs to someone else — only they can answer"
                    : undefined
                }
                onClick={() => (readOnly ? onRefusedInput?.() : void send(option.key))}
              >
                <span className="feed-option-key">{option.key}</span>
                {option.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <form
        className="feed-compose"
        onSubmit={(e) => {
          e.preventDefault()
          if (readOnly) onRefusedInput?.()
          else void send(message, attachments)
        }}
        onDragOver={(e) => {
          if (!readOnly) e.preventDefault()
        }}
        onDrop={(e) => {
          if (readOnly) {
            onRefusedInput?.()
            return
          }
          if (e.dataTransfer.files.length) {
            e.preventDefault()
            addFiles(e.dataTransfer.files)
          }
        }}
      >
        {attachments.length > 0 && (
          <div className="feed-attachments">
            {attachments.map((a) => (
              <div key={a.id} className={`feed-attachment${a.error ? ' error' : ''}`}>
                <img src={a.previewUrl} alt="" />
                {(a.uploading || a.error) && (
                  <div className="feed-attachment-status" title={a.error ?? undefined}>
                    {a.uploading ? (
                      <span className="feed-attachment-spinner" aria-hidden="true" />
                    ) : (
                      <span>!</span>
                    )}
                  </div>
                )}
                <button
                  type="button"
                  className="feed-attachment-remove"
                  onClick={() => removeAttachment(a.id)}
                  aria-label="Remove image"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="feed-compose-row">
          <input
            ref={fileInputRef}
            className="feed-file-input"
            type="file"
            accept="image/*"
            multiple
            tabIndex={-1}
            onChange={(e) => {
              if (e.target.files?.length) addFiles(e.target.files)
              e.target.value = ''
            }}
          />
          <button
            type="button"
            className="feed-attach"
            title={
              readOnly
                ? "This session belongs to someone else — only they can answer"
                : 'Attach an image'
            }
            aria-label="Attach an image"
            disabled={!running}
            onClick={() => (readOnly ? onRefusedInput?.() : fileInputRef.current?.click())}
          >
            +
          </button>
          <textarea
            ref={messageRef}
            className="feed-message"
            rows={1}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              // Shift+Enter is left alone entirely — a plain textarea already
              // inserts the newline on its own, nothing to intervene in here.
              if (e.key !== 'Enter' || e.shiftKey) return
              e.preventDefault()
              if (readOnly) {
                onRefusedInput?.()
                return
              }
              if (!running || sending || attachments.some((a) => a.uploading)) return
              if (!message.trim() && attachments.length === 0) return
              void send(message, attachments)
            }}
            onPaste={(e) => {
              if (readOnly) return
              const files = Array.from(e.clipboardData.files).filter((f) =>
                f.type.startsWith('image/'),
              )
              if (files.length) addFiles(files)
            }}
            placeholder={
              readOnly
                ? 'Read-only — this session is someone else\u2019s'
                : activity === 'working'
                  ? 'Claude is working…'
                  : 'Send a message'
            }
            readOnly={readOnly}
            onClick={() => readOnly && onRefusedInput?.()}
            disabled={!running || sending}
          />
          <button
            className="primary"
            type="submit"
            disabled={
              !running ||
              sending ||
              attachments.some((a) => a.uploading) ||
              (!readOnly && !message.trim() && attachments.length === 0)
            }
          >
            Send
          </button>
          <span className="feed-live-wrap" role="status" aria-live="polite">
            <span
              className={`feed-live${live ? ' on' : ''}`}
              aria-hidden="true"
              title={live ? 'Streaming live' : 'Polling — the live connection is unavailable'}
            />
            <span className="sr-only">
              {live ? 'Live' : 'Not live — checking for updates every few seconds'}
            </span>
            {justWentQuiet && (
              <span className="feed-live-notice" aria-hidden="true">
                Live updates unavailable
              </span>
            )}
          </span>
        </div>
      </form>
    </div>
  )
}

/**
 * A change, sized for a phone.
 *
 * Collapsed to a one-line summary by default: most edits scroll past and only
 * the one you are being asked to approve needs reading. Horizontal scrolling
 * rather than wrapping, because wrapped code stops being scannable.
 */
function Diff({
  lines,
  added,
  removed,
  truncated,
  path,
  startOpen = false,
}: {
  lines: DiffLine[]
  added: number
  removed: number
  truncated: boolean
  path: string
  startOpen?: boolean
}) {
  const [open, setOpen] = useState(startOpen)

  return (
    <div className="diff">
      <button
        className={`diff-head${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="disclose" aria-hidden="true" />
        <span className="diff-path" title={path}>
          {shortPath(path)}
        </span>
        {added > 0 && <span className="diff-added">+{added}</span>}
        {removed > 0 && <span className="diff-removed">−{removed}</span>}
      </button>
      {open && (
        <div className="diff-body">
          {lines.map((line, index) => (
            <div key={index} className={`diff-line diff-${signClass(line.sign)}`}>
              <span className="diff-sign">{line.sign === '@' ? '' : line.sign}</span>
              {line.text}
            </div>
          ))}
          {truncated && (
            <div className="diff-line diff-meta">
              … the rest is not shown; the counts above are for the whole change
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Keep the end of a path, which is the part that identifies the file.
 *
 * Truncating in JS rather than with `direction: rtl`, which flips the leading
 * slash to the end and renders "/api/routes.py" as "api/routes.py/".
 */
function shortPath(path: string, max = 34): string {
  return path.length <= max ? path : '…' + path.slice(-(max - 1))
}

function signClass(sign: string): string {
  if (sign === '+') return 'add'
  if (sign === '-') return 'del'
  if (sign === '@') return 'hunk'
  return 'ctx'
}

/**
 * Reasoning, collapsed to one line until asked for.
 *
 * Expanding in place rather than behind a global toggle: reasoning is usually
 * long enough to bury the conversation on a phone, but occasionally the exact
 * thing you opened the app to read.
 */
function Thinking({ text, dim }: { text: string; dim: string }) {
  const [open, setOpen] = useState(false)
  const firstLine = text.split('\n').find((l) => l.trim()) ?? text

  return (
    <button
      className={`feed-row feed-thinking${dim}${open ? ' open' : ''}`}
      onClick={() => setOpen((v) => !v)}
      title={open ? 'Hide reasoning' : 'Show reasoning'}
    >
      <span className="disclose" aria-hidden="true" />
      <span className="feed-body">{open ? text : firstLine}</span>
    </button>
  )
}

/**
 * A screenshot the agent took, most often while checking a UI change in the
 * browser MCP server it has been given.
 *
 * Shown at a glance, thumbnail-sized, with a click to see it full size — the
 * same disclose pattern as a diff or a thinking block, and no different from
 * viewing an image anywhere else: nothing here reaches into the browser.
 */
function Screenshot({
  mediaType,
  data,
  dim,
}: {
  mediaType: string
  data: string
  dim: string
}) {
  const [open, setOpen] = useState(false)
  const src = `data:${mediaType};base64,${data}`

  return (
    <button
      className={`feed-row feed-screenshot${dim}${open ? ' open' : ''}`}
      onClick={() => setOpen((v) => !v)}
      title={open ? 'Shrink screenshot' : 'View full size'}
    >
      <span className="disclose" aria-hidden="true" />
      <img className="feed-screenshot-img" src={src} alt="Screenshot from the agent's browser" />
    </button>
  )
}

export function FeedRow({ event }: { event: FeedEvent }) {
  const dim = event.sidechain ? ' sidechain' : ''

  if (event.kind === 'tool') {
    if (event.diff?.length) {
      return (
        <div className={`feed-row${dim}`}>
          <Diff
            lines={event.diff}
            added={event.added}
            removed={event.removed}
            truncated={event.truncated}
            path={event.text}
          />
        </div>
      )
    }
    return (
      <div className={`feed-row feed-tool${dim}`}>
        <span className="feed-tool-icon">{TOOL_ICON[event.tool ?? ''] ?? '⏺'}</span>
        <span className="feed-tool-name">{event.tool}</span>
        {event.text && <span className="feed-tool-arg">{event.text}</span>}
      </div>
    )
  }

  if (event.kind === 'result') {
    if (event.image_data) {
      return (
        <Screenshot
          mediaType={event.image_media_type ?? 'image/png'}
          data={event.image_data}
          dim={dim}
        />
      )
    }
    // Successful results are noise on a small screen; failures never are.
    if (event.ok) return null
    return (
      <div className={`feed-row feed-result${dim}`}>
        <span className="feed-tool-icon">✕</span>
        <span className="feed-tool-arg">{event.text}</span>
      </div>
    )
  }

  if (event.kind === 'thinking') {
    return <Thinking text={event.text} dim={dim} />
  }

  return (
    <div className={`feed-row feed-${event.kind}${dim}`}>
      <div className="feed-who">{event.kind === 'user' ? 'You' : 'Claude'}</div>
      <div className="feed-body">{event.text}</div>
    </div>
  )
}
