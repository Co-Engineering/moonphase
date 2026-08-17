import { liveActivity, type Session } from '../lib/api'

interface Props {
  sessions: Session[]
  onOpen: (projectId: string, session: string) => void
}

/**
 * Everything waiting on you, in one place.
 *
 * This is the product's whole claim stated as a screen. The point of moving a
 * session onto a server is that you can stop watching it; the point of the
 * notification is that you find out when that stops being safe. Until now the
 * answer to "is anything waiting for me" was to look down a tree of projects
 * and read the colour of every dot, which is the watching it was supposed to
 * replace.
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

export function Attention({ sessions, onOpen }: Props) {
  const items = waiting(sessions)
  if (items.length === 0) return null

  return (
    <div className="card attention">
      <h2>
        <span className="dot activity-awaiting_input" />
        {items.length === 1 ? 'One session is waiting' : `${items.length} sessions are waiting`}
      </h2>
      <div className="attention-list">
        {items.map((session) => (
          <button
            key={session.id}
            className="attention-row"
            onClick={() => onOpen(session.project_id, session.tmux_session)}
          >
            <span className="attention-where">
              {session.project_name ?? 'project'}
              <span className="attention-session">{session.tmux_session}</span>
            </span>
            {/* The question itself, because "something is waiting" is not
                information you can act on without opening it. */}
            <span className="attention-question">
              {session.activity_detail ?? 'Waiting for input.'}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
