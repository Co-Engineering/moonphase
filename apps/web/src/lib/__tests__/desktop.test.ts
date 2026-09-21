import { afterEach, describe, expect, it } from 'vitest'
import { isDesktop, isMacDesktop, sessionWindowUrl } from '../desktop'
import { forgetHost, rememberHost } from '../host'

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

/**
 * Clicking "Window" in the installed desktop app built a `file://…` URL —
 * `window.location` there is the packaged app's own bundle (loaded via
 * `loadFile()`, see main.ts), nothing to do with the server the user typed
 * in — which the main process's own scheme check then refused to open,
 * exactly the "Refusing to preview a file: URL." a real report showed.
 */
describe('sessionWindowUrl', () => {
  afterEach(() => forgetHost())

  it('uses the configured instance, not the page it was loaded from', () => {
    rememberHost('https://moonphase.example.com')
    const url = sessionWindowUrl('p1', 's1')
    expect(url.startsWith('https://moonphase.example.com/?')).toBe(true)
  })

  it('never builds a URL scheme other than the configured host’s', () => {
    rememberHost('https://moonphase.example.com')
    const url = sessionWindowUrl('p1', 's1')
    expect(new URL(url).protocol).toBe('https:')
  })

  it('carries the project and session as query params', () => {
    rememberHost('https://moonphase.example.com')
    const url = new URL(sessionWindowUrl('proj-1', 'sess-1'))
    expect(url.searchParams.get('window')).toBe('session')
    expect(url.searchParams.get('project')).toBe('proj-1')
    expect(url.searchParams.get('session')).toBe('sess-1')
  })
})
