import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

function ev(over: Partial<FeedEvent> & { id: string; kind: FeedEvent['kind'] }): FeedEvent {
  return {
    text: '', at: null, tool: null, ok: null, sidechain: false,
    diff: null, added: 0, removed: 0, truncated: false,
    image_media_type: null, image_data: null, todos: null,
    ...over,
  }
}

function task(id: string, description = 'research the thing'): FeedEvent {
  return ev({ id, kind: 'tool', tool: 'Task', text: description })
}

function sidechainMsg(id: string, text = 'looking...'): FeedEvent {
  return ev({ id, kind: 'assistant', text, sidechain: true })
}

/**
 * There is no real parent id linking a sidechain run to the Task call that
 * spawned it — see attachSidechainThreads in Feed.tsx for the heuristic and
 * exactly why it has to fail safe rather than guess.
 */
describe('nested sub-agent threads', () => {
  it('attaches a thread to the Task call it followed, collapsed by default', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      task('t1'),
      sidechainMsg('s1', 'step one'),
      sidechainMsg('s2', 'step two'),
      sidechainMsg('s3', 'step three'),
    ])

    const head = await waitFor(() => {
      const el = document.querySelector('.feed-subagent-head')
      if (!el) throw new Error('not rendered yet')
      return el as HTMLElement
    })
    expect(head.textContent).toContain('Task')
    expect(head.textContent).toContain('3 steps')
    // Collapsed: the thread's own content isn't in the DOM yet.
    expect(screen.queryByText('step one')).toBeNull()
    expect(document.querySelectorAll('.feed-row.sidechain')).toHaveLength(0)

    fireEvent.click(head)
    await screen.findByText('step one')
    expect(screen.getByText('step two')).toBeTruthy()
    expect(screen.getByText('step three')).toBeTruthy()
    // Inside an attached thread the wrapper already says "sub-agent
    // content" — the per-row dim would be redundant.
    expect(document.querySelectorAll('.feed-row.sidechain')).toHaveLength(0)
  })

  it('renders a sidechain run with no preceding Task flat, exactly as before', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [sidechainMsg('s1', 'orphaned step')])

    await screen.findByText('orphaned step')
    expect(document.querySelector('.feed-subagent')).toBeNull()
    expect(document.querySelector('.feed-row.sidechain')).not.toBeNull()
  })

  it('two Task calls with nothing resolved between them get no thread — the ambiguous case', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      task('t1', 'first dispatch'),
      task('t2', 'second dispatch'),
      sidechainMsg('s1', 'which one am I from?'),
    ])

    await screen.findByText('which one am I from?')
    // Neither Task claims the run — both stay plain tool rows, and the run
    // renders flat rather than guessing which one it belongs to.
    expect(document.querySelector('.feed-subagent')).toBeNull()
    expect(document.querySelector('.feed-row.sidechain')).not.toBeNull()
    expect(screen.getByText('first dispatch')).toBeTruthy()
    expect(screen.getByText('second dispatch')).toBeTruthy()
  })
})
