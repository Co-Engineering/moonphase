/**
 * Whether to play a sound when one of your sessions starts waiting on you,
 * and which one. A personal, per-device preference — like the sidebar's own
 * display choices, this lives in localStorage rather than the profile.
 */

import { useCallback, useState } from 'react'
import { type SoundChoice } from './sound'

const KEY = 'moonphase.soundAlert'

export interface SoundAlertPreference {
  enabled: boolean
  choice: SoundChoice
}

const DEFAULT_PREFERENCE: SoundAlertPreference = { enabled: false, choice: 'chime' }

export function readSoundAlertPreference(): SoundAlertPreference {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return DEFAULT_PREFERENCE
    const parsed = JSON.parse(raw) as Partial<SoundAlertPreference>
    return {
      enabled: Boolean(parsed.enabled),
      choice: parsed.choice === 'ping' ? 'ping' : 'chime',
    }
  } catch {
    return DEFAULT_PREFERENCE
  }
}

function writeSoundAlertPreference(pref: SoundAlertPreference): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(pref))
  } catch {
    // Private browsing. The choice just won't survive a reload.
  }
}

export function useSoundAlertPreference() {
  const [preference, setPreference] = useState<SoundAlertPreference>(() =>
    readSoundAlertPreference(),
  )

  const update = useCallback((next: Partial<SoundAlertPreference>) => {
    setPreference((current) => {
      const updated = { ...current, ...next }
      writeSoundAlertPreference(updated)
      return updated
    })
  }, [])

  return [preference, update] as const
}
