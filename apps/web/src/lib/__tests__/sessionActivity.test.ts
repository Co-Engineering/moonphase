import { describe, expect, it } from 'vitest'
import { justStartedWaiting } from '../sessionActivity'
import type { Session } from '../api'

const session = (over: Partial<Session> = {}): Session =>
  ({ id: 's1', is_mine: true, ...over }) as unknown as Session

describe('justStartedWaiting', () => {
  it('is false on the first poll, even if already awaiting_input', () => {
    const current = new Map([['s1', 'awaiting_input' as const]])
    expect(justStartedWaiting(null, current, [session()])).toBe(false)
  })

  it('is true when a session transitions into awaiting_input', () => {
    const previous = new Map([['s1', 'working' as const]])
    const current = new Map([['s1', 'awaiting_input' as const]])
    expect(justStartedWaiting(previous, current, [session()])).toBe(true)
  })

  it('is false when a session was already awaiting_input', () => {
    const previous = new Map([['s1', 'awaiting_input' as const]])
    const current = new Map([['s1', 'awaiting_input' as const]])
    expect(justStartedWaiting(previous, current, [session()])).toBe(false)
  })

  it('ignores a transition on a session that is not yours', () => {
    const previous = new Map([['s1', 'working' as const]])
    const current = new Map([['s1', 'awaiting_input' as const]])
    expect(justStartedWaiting(previous, current, [session({ is_mine: false })])).toBe(false)
  })

  it('is false when nothing transitioned', () => {
    const previous = new Map([['s1', 'working' as const]])
    const current = new Map([['s1', 'working' as const]])
    expect(justStartedWaiting(previous, current, [session()])).toBe(false)
  })
})
