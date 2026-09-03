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

function readFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

function writeFlag(key: string, value: boolean): void {
  try {
    if (value) window.localStorage.setItem(key, '1')
    else window.localStorage.removeItem(key)
  } catch {
    // Private browsing. The toggle still works for this session.
  }
}

/** A single on/off preference, persisted under `storageKey`. Same reasoning
 *  as `useCollapsed`, for the one case that isn't a set of ids: whether the
 *  whole sidebar is collapsed, not just something inside it. */
export function useCollapsedFlag(storageKey: string) {
  const [collapsed, setCollapsed] = useState<boolean>(() => readFlag(storageKey))

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      writeFlag(storageKey, next)
      return next
    })
  }, [storageKey])

  return [collapsed, toggle] as const
}
