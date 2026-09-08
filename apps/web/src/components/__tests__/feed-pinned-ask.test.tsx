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

function ev(id: string, kind: FeedEvent['kind'], text: string): FeedEvent {
  return {
    id, kind, text, at: null, tool: null, ok: null, sidechain: false,
    diff: null, added: 0, removed: 0, truncated: false,
    image_media_type: null, image_data: null,
  }
}

/**
 * The terminal has nothing equivalent to pin above a real PTY — this is
 * Feed-specific, which is also why it only needs to track `kind: 'user'`
 * events rather than anything about the terminal at all.
 */
describe('the pinned "last asked" bar', () => {
  it('shows the most recent user message, not an earlier one', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      ev('u1', 'user', 'add a health endpoint'),
      ev('a1', 'assistant', 'sure, one sec'),
      ev('u2', 'user', 'also add a rate limiter'),
    ])

    await waitFor(() => expect(screen.getAllByText('also add a rate limiter').length).toBe(2))
    expect(screen.getAllByText('add a health endpoint')).toHaveLength(1) // history only
    const pinned = document.querySelector('.feed-pinned-ask')
    expect(pinned?.textContent).toContain('also add a rate limiter')
    expect(pinned?.textContent).not.toContain('add a health endpoint')
  })

  it('renders nothing when there is no user message yet', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [ev('a1', 'assistant', 'hello')])

    await screen.findByText('hello')
    expect(document.querySelector('.feed-pinned-ask')).toBeNull()
  })

  it('scrolls to the original message on click', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [ev('u1', 'user', 'add a health endpoint')])
    await waitFor(() => expect(screen.getAllByText('add a health endpoint').length).toBe(2))

    const target = document.getElementById('feed-event-u1') as HTMLElement
    const spy = vi.spyOn(target, 'scrollIntoView')

    ;(document.querySelector('.feed-pinned-ask') as HTMLElement).click()
    expect(spy).toHaveBeenCalled()
  })
})
