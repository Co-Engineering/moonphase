import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * What ends up in a WebSocket URL.
 *
 * A browser cannot set headers on a WebSocket handshake, so proof of identity
 * has to travel in the query string — and a query string is written to every
 * reverse proxy's access log. A real access token there stays valid for an
 * hour; a ticket is single-use and dead in fifteen seconds.
 *
 * The terminal was switched over and the feed was not, which was the wrong way
 * round: the feed socket opens on every project you look at, so it was the one
 * leaking a live token on nearly every page view.
 */

vi.mock('../supabase', () => ({ accessToken: async () => 'a-real-access-token' }))
vi.mock('../host', () => ({ currentHost: () => 'https://moonphase.example.test' }))

afterEach(() => vi.unstubAllGlobals())

function answerTicketWith(ticket: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ ticket }), { status: 200 })),
  )
}

describe.each([
  ['feedUrl', async () => (await import('../api')).feedUrl('p-1')],
  ['terminalUrl', async () => (await import('../api')).terminalUrl('p-1', 80, 24)],
])('%s', (_name, build) => {
  it('carries a ticket and never the access token', async () => {
    answerTicketWith('one-time-ticket')

    const url = await build()

    expect(url).toContain('ticket=one-time-ticket')
    // The whole point. A token here outlives the request that leaked it.
    expect(url).not.toContain('a-real-access-token')
    expect(url).not.toContain('token=')
  })
})
