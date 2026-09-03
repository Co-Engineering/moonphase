import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { useSessionOrder } from '../sessionOrder'
import type { Session } from '../api'

afterEach(() => {
  window.localStorage.clear()
})

const s = (tmux_session: string): Session => ({ tmux_session }) as unknown as Session

describe('useSessionOrder', () => {
  it('leaves the backend order alone until something is dragged', () => {
    const { result } = renderHook(() => useSessionOrder())
    const sessions = [s('a'), s('b'), s('c')]

    expect(result.current.orderedSessions('p1', sessions)).toEqual(sessions)
  })

  it('moves the dragged session to just before the drop target', () => {
    const { result } = renderHook(() => useSessionOrder())
    const sessions = [s('a'), s('b'), s('c')]

    act(() => result.current.moveSession('p1', sessions, 'c', 'a'))

    expect(result.current.orderedSessions('p1', sessions).map((x) => x.tmux_session)).toEqual([
      'c',
      'a',
      'b',
    ])
  })

  it('moves to the end when dropped past the last session', () => {
    const { result } = renderHook(() => useSessionOrder())
    const sessions = [s('a'), s('b'), s('c')]

    act(() => result.current.moveSession('p1', sessions, 'a', null))

    expect(result.current.orderedSessions('p1', sessions).map((x) => x.tmux_session)).toEqual([
      'b',
      'c',
      'a',
    ])
  })

  it('appends a session created after the last reorder, rather than losing or misplacing it', () => {
    const { result } = renderHook(() => useSessionOrder())
    act(() => result.current.moveSession('p1', [s('a'), s('b')], 'b', 'a'))

    // 'c' is new — it was not part of the list when the order was recorded.
    const withNewArrival = [s('a'), s('b'), s('c')]
    expect(
      result.current.orderedSessions('p1', withNewArrival).map((x) => x.tmux_session),
    ).toEqual(['b', 'a', 'c'])
  })

  it('keeps each project s ordering independent', () => {
    const { result } = renderHook(() => useSessionOrder())
    act(() => result.current.moveSession('p1', [s('a'), s('b')], 'b', 'a'))

    expect(
      result.current.orderedSessions('p2', [s('a'), s('b')]).map((x) => x.tmux_session),
    ).toEqual(['a', 'b'])
  })

  it('persists across a remount, the way collapsed state does', () => {
    const first = renderHook(() => useSessionOrder())
    act(() => first.result.current.moveSession('p1', [s('a'), s('b')], 'b', 'a'))

    const second = renderHook(() => useSessionOrder())
    expect(
      second.result.current.orderedSessions('p1', [s('a'), s('b')]).map((x) => x.tmux_session),
    ).toEqual(['b', 'a'])
  })
})
