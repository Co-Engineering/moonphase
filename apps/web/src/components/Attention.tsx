import { useState } from 'react'
import * as api from '../lib/api'
import { liveActivity, type Session } from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * Everything waiting on you, answerable from here.
 *
 * This is the product's whole claim stated as a screen. The point of moving a
 * session onto a server is that you can stop watching it; the point of the
 * notification is that you find out when that stops being safe.
 *
 * The first version got you as far as knowing. Answering still meant opening
 * the project, waiting for a terminal to attach and finding the cursor — which
 * on a phone is most of the work and all of the friction. The question and its
 * options are parsed on the server, so the answer is one tap from the screen
 * the notification already put you on.
 *
 * Only your own sessions appear. Someone else's agent waiting on them is not
 * your problem — you could not answer it if you tried, because it runs on
 * their account.
 */
export function waiting(sessions: Session[]): Session[] {
  return sessions
    .filter((s) => s.is_mine && liveActivity(s) === 'awaiting_input')
    .sort((a, b) => (a.activity_at ?? '').localeCompare(b.activity_at ?? ''))
}

interface Props {
  sessions: Session[]
  onOpen: (projectId: string, session: string) => void
}

export function Attention({ sessions, onOpen }: Props) {
  // The session list already says how many are waiting, and it polls faster.
  // This only fetches the questions, and only when there is one to fetch.
  const anyWaiting = waiting(sessions).length > 0
  const questions = useResource(
    () => (anyWaiting ? api.attention() : Promise.resolve([])),
    [anyWaiting],
    { pollMs: 10000 },
  )

  const items = waiting(sessions)
  if (items.length === 0) return null

  const byKey = new Map(
    (questions.data ?? []).map((item) => [`${item.project_id}:${item.session}`, item]),
  )

  return (
    <div className="card attention">
      <h2>
        <span className="dot activity-awaiting_input" />
        {items.length === 1 ? 'One session is waiting' : `${items.length} sessions are waiting`}
      </h2>
      <div className="attention-list">
        {items.map((session) => (
          <Row
            key={session.id}
            session={session}
            waiting={byKey.get(`${session.project_id}:${session.tmux_session}`)}
            onOpen={() => onOpen(session.project_id, session.tmux_session)}
            onAnswered={() => questions.reload()}
          />
        ))}
      </div>
    </div>
  )
}

function Row({
  session,
  waiting: found,
  onOpen,
  onAnswered,
}: {
  session: Session
  waiting: api.Waiting | undefined
  onOpen: () => void
  onAnswered: () => void
}) {
  const [reply, setReply] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState<string | null>(null)
  const [showTail, setShowTail] = useState(false)

  const question = found?.prompt?.question || found?.question || session.activity_detail
  const options = found?.prompt?.options ?? []

  async function answer(key: string) {
    setBusy(true)
    setError(null)
    try {
      await api.answerSession(session.project_id, session.tmux_session, key)
      setSent(key)
      setReply('')
      onAnswered()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="attention-row">
      <button className="attention-where" onClick={onOpen}>
        {session.project_name ?? 'project'}
        <span className="attention-session">{session.tmux_session}</span>
      </button>

      {/* The question itself, because "something is waiting" is not
          information you can act on without opening it. */}
      <p className="attention-question">{question ?? 'Waiting for input.'}</p>

      {found && (
        <>
          {options.length > 0 ? (
            <div className="answer-options">
              {options.map((option) => (
                <button
                  key={option.key}
                  className="answer-option"
                  disabled={busy}
                  onClick={() => void answer(option.key)}
                  title={option.label}
                >
                  <span className="answer-key">{option.key}</span>
                  {option.label}
                </button>
              ))}
            </div>
          ) : (
            <form
              className="answer-free"
              onSubmit={(event) => {
                event.preventDefault()
                if (reply.trim()) void answer(reply)
              }}
            >
              <input
                value={reply}
                onChange={(event) => setReply(event.target.value)}
                placeholder="Type an answer…"
                disabled={busy}
              />
              <button className="primary" disabled={busy || !reply.trim()}>
                Send
              </button>
            </form>
          )}

          <div className="attention-foot">
            {/* What led to the question. Answering a permission prompt without
                seeing what it is about is how you approve the wrong thing. */}
            <button className="link" onClick={() => setShowTail((on) => !on)}>
              {showTail ? 'Hide' : 'Show'} the last few lines
            </button>
            {sent && <span className="muted">Sent “{sent}”. Waiting for it to move on…</span>}
            {error && <span className="error-inline">{error}</span>}
          </div>
          {showTail && <pre className="attention-tail">{found.tail}</pre>}
        </>
      )}
    </div>
  )
}
