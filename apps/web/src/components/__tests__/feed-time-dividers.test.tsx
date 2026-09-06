import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'
import type { FeedEvent } from '../../lib/api'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

const sockets: StubSocket[] = []

class StubSocket {
  onopen: (() => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor() {
    sockets.push(this)
  }
  close() {}
}

const emptyPage = () =>
  new Response(
    JSON.stringify({ events: [], cursor: '', available: true, activity: 'idle', prompt: null }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )

afterEach(() => {
  vi.unstubAllGlobals()
  sockets.length = 0
})

async function nextSocket(): Promise<StubSocket> {
  await waitFor(() => expect(sockets.length).toBeGreaterThan(0))
  return sockets[0]
}

function pushPage(socket: StubSocket, events: FeedEvent[]) {
  act(() => {
    socket.onmessage?.({ data: JSON.stringify({ type: 'page', events, available: true }) })
  })
}

function msg(id: string, at: string | null, text = 'hi'): FeedEvent {
  return {
    id,
    kind: 'assistant',
    text,
    at,
    tool: null,
    ok: null,
    sidechain: false,
    diff: null,
    added: 0,
    removed: 0,
    truncated: false,
    image_media_type: null,
    image_data: null,
  }
}

/**
 * A stamp before a real gap, not on every row — catching up on a session you
 * stepped away from should show where the time went, without a clock
 * repeated on a burst of messages a second apart.
 */
describe('time dividers in the feed', () => {
  it('shows one divider for a burst of messages close together', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    const base = Date.parse('2026-09-04T12:00:00Z')
    pushPage(await nextSocket(), [
      msg('a', new Date(base).toISOString(), 'first'),
      msg('b', new Date(base + 5_000).toISOString(), 'second'),
      msg('c', new Date(base + 10_000).toISOString(), 'third'),
    ])

    await screen.findByText('first')
    expect(document.querySelectorAll('.feed-time-divider')).toHaveLength(1)
  })

  it('shows a second divider after a real gap', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    const base = Date.parse('2026-09-04T12:00:00Z')
    pushPage(await nextSocket(), [
      msg('a', new Date(base).toISOString(), 'first'),
      // 10 minutes later — a real gap, not the same burst.
      msg('b', new Date(base + 10 * 60_000).toISOString(), 'second'),
    ])

    await screen.findByText('second')
    expect(document.querySelectorAll('.feed-time-divider')).toHaveLength(2)
  })

  it('skips events with no timestamp instead of crashing or stamping them', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [msg('a', null, 'no timestamp here')])

    await screen.findByText('no timestamp here')
    expect(document.querySelectorAll('.feed-time-divider')).toHaveLength(0)
  })
})
