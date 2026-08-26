import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'

/**
 * What an update looks like from the browser.
 *
 * Applying one replaces the API, so requests fail for a few seconds. The app
 * treated that as "this is not a Moonphase host" and presented the connect
 * screen — with an empty field, because someone served the app by the instance
 * has never typed its address and has no reason to know it. A working install
 * asked to be set up again, in the middle of its own update.
 *
 * When the page was served by the instance its address is not in question, so
 * the only sensible answer to a failed request is to ask again.
 */

const config = {
  supabase_url: 'https://auth.example.test',
  supabase_anon_key: 'anon-key-for-tests',
  vapid_public_key: null,
  version: 'v0.7.1',
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('while the API is being replaced', () => {
  it('waits instead of asking where Moonphase lives', async () => {
    // No stored host: this page was served by the instance itself.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    render(<App />)

    await waitFor(() =>
      expect(screen.getByText(/waiting for moonphase to come back/i)).toBeInTheDocument(),
    )
    // The thing that must not happen.
    expect(screen.queryByText(/connect to moonphase/i)).not.toBeInTheDocument()
  })

  it('still asks when the address really is in question', async () => {
    // A host typed in by hand: if that one does not answer, the address is
    // exactly what is in doubt, and the picker is the right screen.
    window.localStorage.setItem('moonphase.host', 'https://typed.example.test')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    render(<App />)

    await waitFor(() =>
      expect(screen.getByText(/connect to moonphase/i)).toBeInTheDocument(),
    )
  })

  it('comes back on its own once the API answers again', async () => {
    let calls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls += 1
        if (calls < 3) throw new TypeError('Failed to fetch')
        const url = String(input)
        if (url.includes('/api/config')) {
          return new Response(JSON.stringify(config), { status: 200 })
        }
        return new Response(JSON.stringify({ session: null }), { status: 200 })
      }),
    )

    render(<App />)

    await waitFor(() =>
      expect(screen.getByText(/waiting for moonphase to come back/i)).toBeInTheDocument(),
    )
    // No reload, no retyping: the retry does it.
    await waitFor(
      () => expect(screen.queryByText(/waiting for moonphase/i)).not.toBeInTheDocument(),
      { timeout: 8000 },
    )
  }, 12000)
})
