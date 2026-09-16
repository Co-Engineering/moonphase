import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'
import type { DiffLine, FeedEvent } from '../../lib/api'

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

function tool(id: string, name: string, over: Partial<FeedEvent> = {}): FeedEvent {
  return {
    id, kind: 'tool', tool: name, text: name, at: null, ok: null, sidechain: false,
    diff: null, added: 0, removed: 0, truncated: false,
    image_media_type: null, image_data: null, todos: null,
    ...over,
  }
}

function failedResult(id: string): FeedEvent {
  return {
    id, kind: 'result', tool: null, text: 'boom', at: null, ok: false, sidechain: false,
    diff: null, added: 0, removed: 0, truncated: false,
    image_media_type: null, image_data: null, todos: null,
  }
}

const A_DIFF: DiffLine[] = [{ sign: '+', text: 'new line' }]

/**
 * A run of tool calls a person would otherwise have to scroll past one at a
 * time, folded into one line — "the current UI is just quite bad" was the
 * verbatim complaint that started this.
 */
describe('collapsed tool-call bursts', () => {
  it('leaves a run of 2 inline — not enough to be worth folding', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [tool('a', 'Read'), tool('b', 'Grep')])

    await waitFor(() => expect(screen.getAllByText(/Read|Grep/).length).toBeGreaterThan(0))
    expect(document.querySelector('.feed-tool-group')).toBeNull()
  })

  it('collapses a run of 3 or more into one line', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      tool('a', 'Read'),
      tool('b', 'Read'),
      tool('c', 'Bash'),
    ])

    const group = await waitFor(() => {
      const el = document.querySelector('.feed-tool-group')
      if (!el) throw new Error('not rendered yet')
      return el
    })
    expect(group.textContent).toContain('3 tool calls')
    expect(group.textContent).toContain('Read ×2')
    expect(group.textContent).toContain('Bash')
    // Collapsed by default: the individual calls aren't in the DOM as
    // their own rows until expanded.
    expect(document.querySelectorAll('.feed-tool-group-body')).toHaveLength(0)

    fireEvent.click(group.querySelector('.feed-tool-group-head') as HTMLElement)
    expect(document.querySelectorAll('.feed-tool-group-body .feed-tool')).toHaveLength(3)
  })

  it('never folds a call carrying a diff into the summary', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      tool('a', 'Read'),
      tool('b', 'Edit', { text: '/x.py', diff: A_DIFF, added: 1 }),
      tool('c', 'Read'),
      tool('d', 'Read'),
    ])

    await waitFor(() => expect(document.querySelector('.diff')).not.toBeNull())
    // The Edit splits what would otherwise be a run of 4 into two runs of
    // 1 — neither reaches the threshold, so nothing collapses at all.
    expect(document.querySelector('.feed-tool-group')).toBeNull()
  })

  it('a failed result interrupts a run', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      tool('a', 'Bash'),
      tool('b', 'Bash'),
      failedResult('r1'),
      tool('c', 'Bash'),
    ])

    await screen.findByText('boom')
    // Two runs of 1 either side of the failure — neither reaches the
    // threshold.
    expect(document.querySelector('.feed-tool-group')).toBeNull()
  })
})
