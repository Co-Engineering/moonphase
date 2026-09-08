/** Whether any of the caller's own sessions just transitioned into
 *  `awaiting_input` between two polls. `previous` being `null` means this is
 *  the first poll — a session that was already waiting when the app opened
 *  should not announce itself as new. */

import type { ActivityState, Session } from './api'

export function justStartedWaiting(
  previous: Map<string, ActivityState> | null,
  current: Map<string, ActivityState>,
  sessions: Session[],
): boolean {
  if (!previous) return false
  return sessions.some(
    (s) =>
      s.is_mine &&
      current.get(s.id) === 'awaiting_input' &&
      previous.get(s.id) !== 'awaiting_input',
  )
}
