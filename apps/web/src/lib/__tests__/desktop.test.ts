import { afterEach, describe, expect, it } from 'vitest'
import { isDesktop, isMacDesktop } from '../desktop'

const win = window as unknown as { moonphase?: { desktop: boolean; platform: string } }

afterEach(() => {
  delete win.moonphase
})

/**
 * Reserving room for macOS's traffic-light buttons (see .mac-inset in
 * styles.css) is only correct when this is actually the desktop shell on
 * actually macOS — a plain browser tab, and the desktop app on Windows or
 * Linux, both have no such overlay and would just lose space for nothing.
 */
describe('isMacDesktop', () => {
  it('is false in a plain browser tab, with no bridge at all', () => {
    expect(isDesktop()).toBe(false)
    expect(isMacDesktop()).toBe(false)
  })

  it('is true only for the desktop shell on darwin', () => {
    win.moonphase = { desktop: true, platform: 'darwin' }
    expect(isMacDesktop()).toBe(true)
  })

  it('is false for the desktop shell on win32 or linux', () => {
    for (const platform of ['win32', 'linux']) {
      win.moonphase = { desktop: true, platform }
      expect(isMacDesktop()).toBe(false)
    }
  })
})
