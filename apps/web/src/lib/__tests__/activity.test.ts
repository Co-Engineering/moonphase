import { describe, expect, it } from 'vitest'
import { ACTIVITY_STALE_AFTER_MS, activityAgo, canControl, checkedAgo, liveActivity } from '../api'

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()

describe('liveActivity', () => {
  it('reports a state that was just confirmed', () => {
    expect(liveActivity({ activity: 'working', checked_at: ago(5_000) })).toBe('working')
  })

  it('refuses to present a stale state as current', () => {
    // The bug this exists for: a session the monitor could not reach kept its
    // last state and was shown with full confidence, so an agent that finished
    // overnight went on displaying a confident blue "working".
    expect(liveActivity({ activity: 'working', checked_at: ago(ACTIVITY_STALE_AFTER_MS + 1000) })).toBe(
      'unknown',
    )
  })

  it('treats never-checked as unknown rather than as truth', () => {
    expect(liveActivity({ activity: 'awaiting_input', checked_at: null })).toBe('unknown')
  })
})

describe('checkedAgo', () => {
  it('is precise while it matters and coarse when it does not', () => {
    expect(checkedAgo({ checked_at: ago(5_000) })).toMatch(/checked \d+s ago/)
    expect(checkedAgo({ checked_at: ago(10 * 60_000) })).toMatch(/min ago/)
    expect(checkedAgo({ checked_at: ago(5 * 3_600_000) })).toMatch(/h ago/)
    expect(checkedAgo({ checked_at: null })).toBe('never checked')
  })
})

describe('activityAgo', () => {
  it('is precise while it matters and coarse when it does not', () => {
    expect(activityAgo({ activity_at: ago(5_000) })).toMatch(/^\d+s ago$/)
    expect(activityAgo({ activity_at: ago(10 * 60_000) })).toMatch(/min ago/)
    expect(activityAgo({ activity_at: ago(5 * 3_600_000) })).toMatch(/h ago/)
    expect(activityAgo({ activity_at: ago(3 * 24 * 3_600_000) })).toMatch(/d ago/)
    expect(activityAgo({ activity_at: null })).toBe('no activity yet')
  })

  it('answers "since when", not "is that still true" — a long-idle session still reads', () => {
    // The bug this guards against: reusing checked_at here would make an
    // always-on, always-idle session read as "5s ago" forever, which says
    // the poll loop is healthy and nothing about when it was last used.
    const longIdleButPolledJustNow = { activity_at: ago(6 * 3_600_000) }
    expect(activityAgo(longIdleButPolledJustNow)).toMatch(/h ago/)
  })
})

describe('canControl', () => {
  it('separates driving from watching', () => {
    expect(canControl('admin')).toBe(true)
    expect(canControl('write')).toBe(true)
    expect(canControl('read')).toBe(false)
    // Owning the machine a project runs on is not owning the project.
    expect(canControl('host')).toBe(false)
  })
})
