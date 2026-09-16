import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  feedUrl,
  uploadSessionFile,
  type DiffLine,
  type FeedEvent,
  type Prompt,
  type TodoItem,
} from '../lib/api'
import { Markdown } from './Markdown'

// Mirrors the backend's own limit (feed.py's and terminal.py's
// _MAX_UPLOAD_BYTES, both 15 MB) so an oversized file is refused here —
// instantly, before it ever leaves the device — rather than after however
// long an upload takes to fail.
const MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
const MAX_ATTACHMENT_MESSAGE = 'Attachments are limited to 15 MB.'

interface Attachment {
  id: string
  file: File
  /** Images get their own preview and land in the session's home, same as
   *  before; anything else has no useful thumbnail and goes to the working
   *  tree instead — see `addFiles`. */
  kind: 'image' | 'file'
  previewUrl?: string
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

// Below this, two timestamps a row apart are not a gap worth marking — the
// same burst of tool calls that always lands together while the agent works.
const TIME_GAP_MS = 60_000

type FeedListRow =
  | { kind: 'event'; event: FeedEvent; thread?: FeedEvent[] }
  | { kind: 'divider'; key: string; label: string }
  | { kind: 'tool-group'; key: string; events: FeedEvent[] }

/** A stamp inserted wherever more than `TIME_GAP_MS` passed since the last
 *  timestamped row — including one before the very first — so scrolling
 *  through history shows where the time actually went, without repeating a
 *  clock on every line. Takes rows rather than raw events so it can run
 *  after `attachSidechainThreads` and leave a thread-carrying row untouched
 *  rather than losing the thread it's carrying. */
function insertTimeDividers(rows: FeedListRow[]): FeedListRow[] {
  const out: FeedListRow[] = []
  let last: number | null = null
  for (const row of rows) {
    if (row.kind !== 'event') {
      out.push(row)
      continue
    }
    const at = row.event.at ? Date.parse(row.event.at) : NaN
    if (!Number.isNaN(at) && (last === null || at - last > TIME_GAP_MS)) {
      out.push({ kind: 'divider', key: `t-${row.event.id}`, label: formatEventTime(at) })
    }
    if (!Number.isNaN(at)) last = at
    out.push(row)
  }
  return out
}

/** A run of at least this many plain tool calls in a row collapses into one
 *  "N tool calls" line — a judgment call about what reads as a burst worth
 *  folding away, not a technical constraint. */
const TOOL_BURST_MIN_SIZE = 3

/**
 * Pairs a `Task` tool call with the sidechain (sub-agent) events that follow
 * it, so the feed can show them as one collapsible thread instead of a run
 * of individually dimmed rows interleaved with the main conversation.
 *
 * There is no real parent id linking a sidechain run to the Task call that
 * spawned it (Claude Code's transcript records a flat `isSidechain` flag and
 * nothing else) — this is a heuristic: the most recent unclaimed `Task` call
 * claims the very next run of sidechain events. It fails safe rather than
 * guessing wrong: a sidechain run with no preceding Task call (cut off by
 * `MAX_EVENTS`, say), or two `Task` calls with nothing resolved between them
 * (which Task would the next run even belong to?), both fall through to
 * today's flat, individually dimmed rows rather than attaching to the wrong
 * thread.
 */
function attachSidechainThreads(events: FeedEvent[]): FeedListRow[] {
  const rows: FeedListRow[] = []
  let pendingTask: number | 'ambiguous' | null = null
  let i = 0
  while (i < events.length) {
    const event = events[i]
    if (event.sidechain) {
      const run: FeedEvent[] = []
      while (i < events.length && events[i].sidechain) {
        run.push(events[i])
        i++
      }
      if (typeof pendingTask === 'number') {
        const claimed = rows[pendingTask]
        if (claimed.kind === 'event') claimed.thread = run
      } else {
        for (const sidechainEvent of run) rows.push({ kind: 'event', event: sidechainEvent })
      }
      pendingTask = null
      continue
    }
    rows.push({ kind: 'event', event })
    if (event.kind === 'tool' && event.tool === 'Task') {
      pendingTask = pendingTask === null ? rows.length - 1 : 'ambiguous'
    }
    i++
  }
  return rows
}

/**
 * Folds a run of `TOOL_BURST_MIN_SIZE`+ plain tool calls into one collapsed
 * row. A call carrying a diff is never folded in — an Edit or Write worth
 * approving on its own merits must stay visible, not buried in a summary
 * someone has to expand to notice. A row already carrying a sidechain
 * thread is left alone too: it renders as its own `SubagentThread`, not a
 * plain tool line, so it shouldn't be swallowed into a burst summary either.
 */
function groupToolBursts(rows: FeedListRow[]): FeedListRow[] {
  const out: FeedListRow[] = []
  let run: FeedEvent[] = []
  const flush = () => {
    if (run.length >= TOOL_BURST_MIN_SIZE) {
      out.push({ kind: 'tool-group', key: `g-${run[0].id}`, events: run })
    } else {
      for (const event of run) out.push({ kind: 'event', event })
    }
    run = []
  }
  for (const row of rows) {
    if (row.kind === 'event' && !row.thread && row.event.kind === 'tool' && !row.event.diff?.length) {
      run.push(row.event)
      continue
    }
    flush()
    out.push(row)
  }
  flush()
  return out
}

function formatEventTime(at: number): string {
  const d = new Date(at)
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return time
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`
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
    for (const a of list) if (a.previewUrl) URL.revokeObjectURL(a.previewUrl)
  }, [])
  const attachmentsRef = useRef<Attachment[]>([])
  useEffect(() => {
    attachmentsRef.current = attachments
  }, [attachments])
  useEffect(() => () => revokeAttachments(attachmentsRef.current), [revokeAttachments])

  const addFiles = useCallback(
    (files: Iterable<File>) => {
      const incoming = Array.from(files)
      if (!incoming.length) return
      const next: Attachment[] = incoming.map((file) => {
        const isImage = file.type.startsWith('image/')
        const tooLarge = file.size > MAX_ATTACHMENT_BYTES
        return {
          id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
          file,
          kind: isImage ? 'image' : 'file',
          previewUrl: isImage ? URL.createObjectURL(file) : undefined,
          path: null,
          uploading: !tooLarge,
          error: tooLarge ? MAX_ATTACHMENT_MESSAGE : null,
        }
      })
      setAttachments((current) => [...current, ...next])
      for (const attachment of next) {
        if (attachment.error) continue // too large — nothing to upload
        // Images land in the session's home (feed/upload) — a reference a
        // message points at, not meant to show up as an untracked file in
        // `git status`. Anything else goes into the working tree instead,
        // through the same endpoint the terminal's own upload button uses:
        // a spec, a CSV or a config file dropped here is meant to become
        // part of the project, not just be glanced at once.
        const upload =
          attachment.kind === 'image'
            ? api.uploadFeedImage(projectId, attachment.file, session)
            : uploadSessionFile(projectId, attachment.file, session)
        upload
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

  // Sidechain runs are paired with the Task call that spawned them, then a
  // stamp is inserted before a real time gap, then a run of plain tool
  // calls folds into one line — each pass builds on the last, so order
  // matters: pairing needs the raw event list, dividers need to skip over
  // a thread rather than splitting it, and grouping must not fold a row
  // that a divider or a thread already claimed.
  const rows = useMemo(
    () => groupToolBursts(insertTimeDividers(attachSidechainThreads(events))),
    [events],
  )

  // The most recent TodoWrite call's own checklist — a sub-agent's is
  // excluded on purpose, since its scratch list is not "the plan" for the
  // session as a whole.
  const latestTodos = useMemo(
    () =>
      [...events]
        .reverse()
        .find((e) => e.kind === 'tool' && e.tool === 'TodoWrite' && !e.sidechain && e.todos?.length)
        ?.todos ?? null,
    [events],
  )

  // What you last asked for, pinned above the scroll so it survives being
  // buried under everything the agent did in response — the terminal has no
  // equivalent (there's nothing to pin above a real PTY), which is exactly
  // why this exists here instead.
  const lastUserMessage = useMemo(
    () => [...events].reverse().find((e) => e.kind === 'user'),
    [events],
  )
  const scrollToEvent = useCallback((id: string) => {
    document.getElementById(`feed-event-${id}`)?.scrollIntoView({ block: 'start' })
  }, [])

  return (
    <div className="feed">
      <div className="feed-scroll" ref={scrollerRef} onScroll={onScroll}>
        {(latestTodos || lastUserMessage) && (
          <div className="feed-pinned">
            {latestTodos && <TodoChecklist todos={latestTodos} />}
            {lastUserMessage && (
              <button
                type="button"
                className="feed-pinned-ask"
                title="Jump to this message"
                onClick={() => scrollToEvent(lastUserMessage.id)}
              >
                <span className="feed-pinned-label">Last asked</span>
                <span className="feed-pinned-text">{lastUserMessage.text}</span>
              </button>
            )}
          </div>
        )}
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
          rows.map((row) =>
            row.kind === 'divider' ? (
              <div className="feed-time-divider" key={row.key}>
                {row.label}
              </div>
            ) : row.kind === 'tool-group' ? (
              <ToolGroup key={row.key} events={row.events} />
            ) : row.thread ? (
              <div key={row.event.id} id={`feed-event-${row.event.id}`}>
                <SubagentThread taskEvent={row.event} thread={row.thread} />
              </div>
            ) : (
              <div key={row.event.id} id={`feed-event-${row.event.id}`}>
                <FeedRow event={row.event} />
              </div>
            ),
          )
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
              <div
                key={a.id}
                className={`feed-attachment${a.kind === 'file' ? ' file' : ''}${a.error ? ' error' : ''}`}
              >
                {a.kind === 'image' ? (
                  <img src={a.previewUrl} alt="" />
                ) : (
                  <span className="feed-attachment-file" title={a.file.name}>
                    {a.file.name}
                  </span>
                )}
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
                  aria-label={a.kind === 'image' ? 'Remove image' : 'Remove file'}
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
                : prompt
                  ? 'Answer the question above first'
                  : 'Attach a file'
            }
            aria-label="Attach a file"
            disabled={!running || !!prompt}
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
              if (!running || sending || prompt || attachments.some((a) => a.uploading)) return
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
                : prompt
                  ? 'Tap a choice above to answer'
                  : activity === 'working'
                    ? 'Claude is working…'
                    : 'Send a message'
            }
            readOnly={readOnly}
            onClick={() => readOnly && onRefusedInput?.()}
            disabled={!running || sending || !!prompt}
          />
          <button
            className="primary"
            type="submit"
            disabled={
              !running ||
              sending ||
              !!prompt ||
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

const TODO_STATUS_GLYPH: Record<TodoItem['status'], string> = {
  completed: '✓',
  in_progress: '◐',
  pending: '○',
}

/**
 * The current plan, pinned above the scroll — collapsed to a progress count
 * and whatever's active, same disclosure pattern as `Diff`/`Thinking`. A
 * sub-agent's own TodoWrite calls never reach this component (see
 * `latestTodos` in `Feed`), so what's shown here is always the main
 * conversation's plan, not one narrower slice of it.
 */
function TodoChecklist({ todos }: { todos: TodoItem[] }) {
  const [open, setOpen] = useState(false)
  const done = todos.filter((t) => t.status === 'completed').length
  const current = todos.find((t) => t.status === 'in_progress') ?? todos.find((t) => t.status === 'pending')

  return (
    <div className="feed-todos">
      <button
        type="button"
        className={`feed-todos-head${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        title={open ? 'Hide the plan' : 'Show the plan'}
      >
        <span className="disclose" aria-hidden="true" />
        <span className="feed-todos-progress">
          {done}/{todos.length}
        </span>
        {!open && current && <span className="feed-todos-current">{current.content}</span>}
      </button>
      {open && (
        <div className="feed-todos-list">
          {todos.map((todo, index) => (
            <div
              key={index}
              className={`feed-todo-item${
                todo.status === 'completed' ? ' done' : todo.status === 'in_progress' ? ' active' : ''
              }`}
            >
              <span className="feed-todo-status" aria-hidden="true">
                {TODO_STATUS_GLYPH[todo.status]}
              </span>
              {todo.content}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * A run of `TOOL_BURST_MIN_SIZE`+ plain tool calls, collapsed to one line —
 * expanding it renders each original event through the ordinary `FeedRow`,
 * so nothing about how a single call looks is duplicated here.
 */
function ToolGroup({ events }: { events: FeedEvent[] }) {
  const [open, setOpen] = useState(false)
  const counts = new Map<string, number>()
  for (const event of events) {
    const name = event.tool ?? 'tool'
    counts.set(name, (counts.get(name) ?? 0) + 1)
  }
  const summary = [...counts.entries()]
    .map(([name, count]) => (count > 1 ? `${name} ×${count}` : name))
    .join(', ')

  return (
    <div className="feed-tool-group">
      <button
        type="button"
        className={`feed-tool-group-head${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="disclose" aria-hidden="true" />
        <span className="feed-tool-icon">⏺</span>
        <span className="feed-tool-name">{events.length} tool calls</span>
        <span className="feed-tool-arg">{summary}</span>
      </button>
      {open && (
        <div className="feed-tool-group-body">
          {events.map((event) => (
            <FeedRow key={event.id} event={event} />
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * A `Task` call and the sub-agent run it spawned, collapsed by default —
 * exactly the kind of thing that should stay out of the way until asked
 * for, same as today's dimmed-not-hidden treatment of sidechain traffic.
 * The thread's own events get the same time-divider and tool-burst
 * treatment the top level gets, recursively, so a busy sub-agent reads as
 * easily as the main conversation does.
 */
function SubagentThread({ taskEvent, thread }: { taskEvent: FeedEvent; thread: FeedEvent[] }) {
  const [open, setOpen] = useState(false)
  const threadRows = useMemo(
    () => groupToolBursts(insertTimeDividers(thread.map((event) => ({ kind: 'event' as const, event })))),
    [thread],
  )

  return (
    <div className="feed-subagent">
      <button
        type="button"
        className={`feed-subagent-head${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="disclose" aria-hidden="true" />
        <ToolLine tool={taskEvent.tool} text={taskEvent.text} />
        <span className="feed-subagent-count">
          {thread.length} step{thread.length === 1 ? '' : 's'}
        </span>
      </button>
      {open && (
        <div className="feed-subagent-body">
          {threadRows.map((row) =>
            row.kind === 'divider' ? (
              <div className="feed-time-divider" key={row.key}>
                {row.label}
              </div>
            ) : row.kind === 'tool-group' ? (
              <ToolGroup key={row.key} events={row.events} />
            ) : (
              <FeedRow key={row.event.id} event={row.event} suppressSidechainDim />
            ),
          )}
        </div>
      )}
    </div>
  )
}

/** The icon+name+arg content of one tool-call row, shared by `FeedRow`'s own
 *  plain tool rows, a collapsed `SubagentThread`'s header (the Task call
 *  that spawned it), and — via the same classes — `ToolGroup`'s summary. */
function ToolLine({ tool, text }: { tool: string | null; text: string }) {
  return (
    <>
      <span className="feed-tool-icon">{TOOL_ICON[tool ?? ''] ?? '⏺'}</span>
      <span className="feed-tool-name">{tool}</span>
      {text && <span className="feed-tool-arg">{text}</span>}
    </>
  )
}

export function FeedRow({
  event,
  suppressSidechainDim = false,
}: {
  event: FeedEvent
  /** True only from inside a `SubagentThread` — its own wrapper already
   *  says "this is sub-agent content," so dimming every row inside it too
   *  is redundant. A row shown flat (no thread could be attached to it)
   *  keeps the dim, since it's the only signal it has. */
  suppressSidechainDim?: boolean
}) {
  const dim = event.sidechain && !suppressSidechainDim ? ' sidechain' : ''

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
        <ToolLine tool={event.tool} text={event.text} />
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

  // Your own typed message is shown exactly as typed — it is input, not a
  // reply, and re-interpreting a stray `*` or `_` in it as formatting would
  // make it look different from what was actually sent. The agent's side is
  // real markdown and reads far better rendered as such.
  const isUser = event.kind === 'user'
  return (
    <div className={`feed-row feed-${event.kind}${dim}`}>
      <div className="feed-who">{isUser ? 'You' : 'Claude'}</div>
      <div className="feed-body">{isUser ? event.text : <Markdown text={event.text} />}</div>
    </div>
  )
}
