import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'

/**
 * Does the app render at all.
 *
 * Written after shipping a blank window: a hook placed below an early return
 * ran on some renders and not others, React tore the tree down when the count
 * changed, and nothing on screen said why. `tsc` was happy, the build was
 * happy, and the only symptom was white.
 *
 * These mount the real component through its real boot sequence — fetch the
 * host config, build the auth client, decide what to show — with only the
 * network stubbed. Any crash in that path fails here rather than in front of
 * someone.
 */

const config = {
  supabase_url: 'https://auth.example.test',
  supabase_anon_key: 'anon-key-for-tests',
  vapid_public_key: null,
  version: '0.1.0',
}

function answerWith(handler: (url: string) => unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = handler(url)
      if (body === undefined) throw new Error(`unexpected request: ${url}`)
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('App', () => {
  it('boots to sign-in when the host answers', async () => {
    answerWith((url) => (url.includes('/api/config') ? config : { session: null }))

    render(<App />)

    // Getting this far means the whole boot path ran without the hook order
    // changing between renders, which is the failure being guarded against.
    await waitFor(() => expect(screen.getByText(/sign in/i)).toBeInTheDocument())
  })

  it('asks for a host when there is none to be found', async () => {
    // A fresh install on a phone, where the app was not served by the API.
    window.localStorage.setItem('moonphase.host', 'https://gone.example.test')
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
    expect(screen.getByPlaceholderText(/example\.com/i)).toBeInTheDocument()
  })

  it('survives the config resolving after the first paint', async () => {
    // The exact sequence that broke: render once with no config, then again
    // with one. A hook below an early return changes count between those two
    // and React unmounts everything.
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      release = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes('/api/config')) {
          await pending
          return new Response(JSON.stringify(config), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return new Response('{}', { status: 200 })
      }),
    )

    render(<App />)
    expect(screen.getByText(/connecting/i)).toBeInTheDocument()

    release(null)
    await waitFor(() => expect(screen.getByText(/sign in/i)).toBeInTheDocument())
  })
})
