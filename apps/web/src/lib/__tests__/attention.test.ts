import { describe, expect, it } from 'vitest'
import { waiting } from '../../components/Attention'
import type { Session } from '../api'

const base = (over: Partial<Session>): Session =>
  ({
    id: 'x', project_id: 'p', tmux_session: 's', harness: 'claude_code',
    state: 'running', started_at: null, last_attached_at: null,
    transcript_path: null, activity: 'idle', activity_detail: null,
    activity_at: null, checked_at: new Date().toISOString(),
    user_id: 'u', owner: null, is_mine: true, project_name: 'proj',
    workdir: '/workspace', branch: null, attached_clients: 0, alive: true,
    ...over,
  }) as Session

describe('waiting', () => {
  it('lists only sessions actually waiting on an answer', () => {
    const items = waiting([
      base({ id: 'a', activity: 'awaiting_input' }),
      base({ id: 'b', activity: 'working' }),
      base({ id: 'c', activity: 'idle' }),
    ])
    expect(items.map((s) => s.id)).toEqual(['a'])
  })

  it('ignores other people’s sessions', () => {
    // You could not answer one if you tried: it runs on their account, and the
    // server refuses your keystrokes. Listing it would be an alarm with no
    // action attached.
    const items = waiting([
      base({ id: 'mine', activity: 'awaiting_input', is_mine: true }),
      base({ id: 'theirs', activity: 'awaiting_input', is_mine: false }),
    ])
    expect(items.map((s) => s.id)).toEqual(['mine'])
  })

  it('ignores a state nobody has confirmed lately', () => {
    // The same staleness rule as the sidebar dot. An unreachable session that
    // was waiting hours ago is not evidence that it still is, and a false
    // "answer me" is worse than silence.
    const stale = new Date(Date.now() - 60 * 60_000).toISOString()
    const items = waiting([
      base({ id: 'stale', activity: 'awaiting_input', checked_at: stale }),
    ])
    expect(items).toEqual([])
  })

  it('puts the longest wait first', () => {
    const items = waiting([
      base({ id: 'recent', activity: 'awaiting_input', activity_at: '2026-08-17T10:00:00Z' }),
      base({ id: 'ancient', activity: 'awaiting_input', activity_at: '2026-08-17T08:00:00Z' }),
    ])
    expect(items.map((s) => s.id)).toEqual(['ancient', 'recent'])
  })
})
