import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { readSoundAlertPreference, useSoundAlertPreference } from '../soundAlertPreference'

afterEach(() => {
  window.localStorage.clear()
})

describe('sound alert preference', () => {
  it('defaults to off, chime', () => {
    expect(readSoundAlertPreference()).toEqual({ enabled: false, choice: 'chime' })
  })

  it('persists a change across a fresh read', () => {
    const { result } = renderHook(() => useSoundAlertPreference())
    act(() => result.current[1]({ enabled: true, choice: 'ping' }))

    expect(readSoundAlertPreference()).toEqual({ enabled: true, choice: 'ping' })
  })

  it('updates one field without clobbering the other', () => {
    const { result } = renderHook(() => useSoundAlertPreference())
    act(() => result.current[1]({ enabled: true }))
    act(() => result.current[1]({ choice: 'ping' }))

    expect(result.current[0]).toEqual({ enabled: true, choice: 'ping' })
  })

  it('falls back to defaults for corrupt storage instead of throwing', () => {
    window.localStorage.setItem('moonphase.soundAlert', 'not json')
    expect(readSoundAlertPreference()).toEqual({ enabled: false, choice: 'chime' })
  })
})
