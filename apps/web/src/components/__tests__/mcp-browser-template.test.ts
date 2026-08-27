import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The Browser template's command line.
 *
 * `@playwright/mcp` defaults to the *chrome channel* — a system Google Chrome
 * at /opt/google/chrome/chrome — not the Chromium that Playwright installs.
 * Nothing here installs Google Chrome, so without `--browser chromium` the
 * server connects perfectly and then fails on first use with "Chromium isn't
 * installed at the expected path", which reads like a broken container rather
 * than a missing flag.
 *
 * Read out of the source rather than imported, because the value lives in a
 * module that pulls in the whole settings UI; what matters is the flag being
 * there, next to the environment variable that only helps if it is.
 */
const source = readFileSync(
  resolve(process.cwd(), 'src/components/ClaudeConfig.tsx'),
  'utf8',
)

const browserTemplate =
  source.slice(source.indexOf("label: 'Browser'"), source.indexOf("label: 'Remote (HTTP)'"))

describe('the Browser MCP template', () => {
  it('asks for chromium rather than the chrome channel', () => {
    expect(browserTemplate).toContain('--browser chromium')
  })

  it('points at where the browser environment installs it', () => {
    // The flag and the path only work together: one says which browser, the
    // other says where to find it.
    expect(browserTemplate).toContain('PLAYWRIGHT_BROWSERS_PATH')
    expect(browserTemplate).toContain('/opt/playwright-browsers')
  })

  it('says which environment it needs', () => {
    // A server that cannot work in this project should say so before it is
    // added, not after an agent tries to use it.
    expect(browserTemplate).toMatch(/needs:/)
    expect(browserTemplate).toContain('browser tools')
  })
})
