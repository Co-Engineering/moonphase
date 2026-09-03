/**
 * The order sessions appear in within a project's sidebar entry.
 *
 * Purely a display preference, same reasoning as `collapsed.ts`: nobody
 * needs to see how you like your own sidebar arranged, so this lives in
 * localStorage rather than the database. Stored as an explicit list of
 * `tmux_session` names per project; anything not in that list (a session
 * created after the last reorder) falls in at the end, in whatever order
 * the backend already sorted it — so a fresh session never jumps to some
 * arbitrary spot in the middle.
 */

import { useCallback, useState } from 'react'
import type { Session } from './api'

const STORAGE_KEY = 'moonphase.sessionOrder'

type OrderMap = Record<string, string[]>

function read(): OrderMap {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as OrderMap) : {}
  } catch {
    return {}
  }
}

function write(value: OrderMap): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  } catch {
    // Private browsing. Reordering still works for this session.
  }
}

export function useSessionOrder() {
  const [orders, setOrders] = useState<OrderMap>(() => read())

  const orderedSessions = useCallback(
    (projectId: string, sessions: Session[]): Session[] => {
      const remembered = orders[projectId]
      if (!remembered?.length) return sessions
      const byName = new Map(sessions.map((s) => [s.tmux_session, s]))
      const ordered: Session[] = []
      for (const name of remembered) {
        const session = byName.get(name)
        if (session) {
          ordered.push(session)
          byName.delete(name)
        }
      }
      // Anything left over is new since the last reorder — append it in the
      // order the backend gave it, rather than losing it or guessing.
      for (const session of sessions) {
        if (byName.has(session.tmux_session)) ordered.push(session)
      }
      return ordered
    },
    [orders],
  )

  /** Move `draggedName` to just before `beforeName` (or to the end if null). */
  const moveSession = useCallback(
    (projectId: string, sessions: Session[], draggedName: string, beforeName: string | null) => {
      if (draggedName === beforeName) return
      setOrders((current) => {
        const currentOrder = orderedSessions(projectId, sessions).map((s) => s.tmux_session)
        const without = currentOrder.filter((name) => name !== draggedName)
        const insertAt = beforeName ? without.indexOf(beforeName) : without.length
        const next = [...without]
        next.splice(insertAt < 0 ? without.length : insertAt, 0, draggedName)
        const updated = { ...current, [projectId]: next }
        write(updated)
        return updated
      })
    },
    [orderedSessions],
  )

  return { orderedSessions, moveSession }
}
