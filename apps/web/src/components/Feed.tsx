import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type FeedEvent, type Prompt } from '../lib/api'

interface Props {
  projectId: string
  session: string
  running: boolean
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
export function Feed({ projectId, session, running }: Props) {
  const [events, setEvents] = useState<FeedEvent[]>([])
  const [prompt, setPrompt] = useState<Prompt | null>(null)
  const [activity, setActivity] = useState('unknown')
  const [available, setAvailable] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)

  const cursorRef = useRef<string>('')
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  // Only follow new output when the reader is already at the bottom; yanking
  // the view while someone is reading history is worse than a missed update.
  const pinnedRef = useRef(true)

  const poll = useCallback(async () => {
    try {
      const page = await api.feed(projectId, session, cursorRef.current || undefined)
      cursorRef.current = page.cursor
      setAvailable(page.available)
      setActivity(page.activity)
      setPrompt(page.prompt)
      setError(null)
      if (page.events.length > 0) {
        setEvents((current) => {
          // The cursor makes duplicates unlikely, but a retried request or a
          // rotated transcript can replay; ids make that harmless.
          const seen = new Set(current.map((e) => e.id))
          const fresh = page.events.filter((e) => !seen.has(e.id))
          return fresh.length ? [...current, ...fresh].slice(-600) : current
        })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [projectId, session])

  // Restart the feed when the project or session changes.
  useEffect(() => {
    cursorRef.current = ''
    setEvents([])
    setPrompt(null)
    pinnedRef.current = true
  }, [projectId, session])

  useEffect(() => {
    if (!running) return
    void poll()
    const id = window.setInterval(() => void poll(), 3000)
    return () => window.clearInterval(id)
  }, [poll, running])

  useEffect(() => {
    if (pinnedRef.current) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [events, prompt])

  const onScroll = () => {
    const el = scrollerRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  const send = async (text: string) => {
    if (!text.trim()) return
    setSending(true)
    setError(null)
    try {
      await api.answerFeed(projectId, text, session)
      setMessage('')
      // Answering usually changes things immediately; waiting for the next
      // tick makes the UI feel unresponsive.
      window.setTimeout(() => void poll(), 700)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

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
          <div className="feed-question">{prompt.question}</div>
          <div className="feed-options">
            {prompt.options.map((option) => (
              <button
                key={option.key}
                className={option.key === '1' ? 'primary' : ''}
                disabled={sending}
                onClick={() => void send(option.key)}
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
          void send(message)
        }}
      >
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder={
            activity === 'working' ? 'Claude is working…' : 'Send a message'
          }
          disabled={!running || sending}
        />
        <button className="primary" type="submit" disabled={!running || sending || !message.trim()}>
          Send
        </button>
      </form>
    </div>
  )
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
      <span className="feed-thinking-mark">{open ? '▾' : '▸'}</span>
      <span className="feed-body">{open ? text : firstLine}</span>
    </button>
  )
}

function FeedRow({ event }: { event: FeedEvent }) {
  const dim = event.sidechain ? ' sidechain' : ''

  if (event.kind === 'tool') {
    return (
      <div className={`feed-row feed-tool${dim}`}>
        <span className="feed-tool-icon">{TOOL_ICON[event.tool ?? ''] ?? '⏺'}</span>
        <span className="feed-tool-name">{event.tool}</span>
        {event.text && <span className="feed-tool-arg">{event.text}</span>}
      </div>
    )
  }

  if (event.kind === 'result') {
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
