import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from '../clipboard'

/**
 * Every copy button in the app was broken on any instance without HTTPS.
 *
 * `navigator.clipboard` exists only in a secure context, so on an instance
 * reached by IP over plain HTTP — which is every instance before somebody
 * points a domain at it — the object is undefined and calling `writeText` on it
 * throws. The throw went into a `void`, so the button did nothing and said
 * nothing. The one that mattered was the harness sign-in URL, which is far too
 * long to retype.
 */
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('copying text', () => {
  it('uses the modern API when there is one', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })

    expect(await copyText('https://claude.com/oauth')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('https://claude.com/oauth')
  })

  it('falls back when there is no clipboard object at all', async () => {
    // Exactly what a page served over plain HTTP sees.
    vi.stubGlobal('navigator', {})
    const exec = vi.fn().mockReturnValue(true)
    document.execCommand = exec

    expect(await copyText('https://claude.com/oauth')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('falls back when the modern API exists and refuses', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    const exec = vi.fn().mockReturnValue(true)
    document.execCommand = exec

    expect(await copyText('something')).toBe(true)
    expect(exec).toHaveBeenCalled()
  })

  it('reports failure rather than pretending', async () => {
    // The caller shows "select it and copy manually", which is only possible if
    // it is told.
    vi.stubGlobal('navigator', {})
    document.execCommand = vi.fn().mockReturnValue(false)

    expect(await copyText('something')).toBe(false)
  })

  it('says no to nothing', async () => {
    expect(await copyText('')).toBe(false)
  })
})
