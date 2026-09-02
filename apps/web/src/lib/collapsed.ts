/**
 * Which servers and projects are collapsed in the sidebar tree.
 *
 * Purely a display preference — nothing here is read by the backend — so it
 * lives in localStorage rather than a profile column, and quietly falls back
 * to "nothing collapsed" wherever storage is unavailable.
 */

import { useCallback, useState } from 'react'

function read(key: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set()
  } catch {
    return new Set()
  }
}

function write(key: string, value: Set<string>): void {
  try {
    window.localStorage.setItem(key, JSON.stringify([...value]))
  } catch {
    // Private browsing. The toggle still works for this session.
  }
}

/** A set of ids, toggled one at a time and persisted under `storageKey`. */
export function useCollapsed(storageKey: string) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => read(storageKey))

  const toggle = useCallback(
    (id: string) => {
      setCollapsed((prev) => {
        const next = new Set(prev)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        write(storageKey, next)
        return next
      })
    },
    [storageKey],
  )

  return [collapsed, toggle] as const
}
