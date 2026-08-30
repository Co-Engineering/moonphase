import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * openPreview hands a real bearer token to whatever `apiUrl` it is given,
 * in an Authorization header. There is no fixed "the" API host to compare
 * that against — this app connects to whichever self-hosted server the user
 * typed in — so the shell instead remembers what the renderer announced it
 * is talking to, and refuses an apiUrl that does not match.
 *
 * The desktop package has no test runner of its own, so this reads the
 * source, the same way the Browser MCP template's test does. It is a guard
 * against the check being dropped, not a substitute for exercising Electron:
 * what it pins is that the refusal exists, is anchored to the announced
 * host, and that the announcement is what the renderer actually sends.
 */
const main = readFileSync(resolve(process.cwd(), '../desktop/src/main.ts'), 'utf8')
const preload = readFileSync(resolve(process.cwd(), '../desktop/src/preload.ts'), 'utf8')
const desktopLib = readFileSync(resolve(process.cwd(), 'src/lib/desktop.ts'), 'utf8')

describe('relaying a bearer token to a preview host', () => {
  it('refuses an apiUrl that is not the announced one', () => {
    const fn = main.slice(main.indexOf('async function openPreview'), main.indexOf('const relay'))
    // The comparison itself, not a comment mentioning it: the first version
    // of this test asserted only that the name appeared somewhere in the
    // function, which the explanatory comment above the check satisfied all
    // on its own — deleting the check left it green.
    expect(fn).toContain('sameOrigin(request.apiUrl, configuredApiHost)')
  })

  it('compares by origin rather than by string prefix', () => {
    expect(main).toContain('new URL(a).origin === new URL(b).origin')
  })

  it('refuses when nothing has been announced yet', () => {
    // `!configuredApiHost ||` — the null case must fail closed, not skip
    // the check and fall through to relaying the token.
    expect(main).toMatch(/!configuredApiHost\s*\|\|/)
  })

  it('only records an address that passes the same validation', () => {
    const handler = main.slice(main.indexOf("ipcMain.on('host:set-current'"))
    expect(handler.slice(0, 200)).toContain('validApiUrl')
  })

  it('is announced across the bridge and from the renderer', () => {
    expect(preload).toContain('host:set-current')
    expect(desktopLib).toContain('setApiHost')
  })
})
