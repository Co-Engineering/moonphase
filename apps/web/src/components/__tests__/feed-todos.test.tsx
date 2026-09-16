import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'
import type { FeedEvent, TodoItem } from '../../lib/api'

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

function todoWrite(id: string, todos: TodoItem[], over: Partial<FeedEvent> = {}): FeedEvent {
  return ev({ id, kind: 'tool', tool: 'TodoWrite', text: '', todos, ...over })
}

/**
 * The pinned plan checklist (issue: "the current UI is just quite bad" —
 * TodoWrite calls previously rendered as a blank tool line with nothing
 * useful to show).
 */
describe('the pinned todo checklist', () => {
  it('shows the latest TodoWrite snapshot, not an earlier one', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      todoWrite('t1', [{ content: 'Old plan item', status: 'pending' }]),
      todoWrite('t2', [
        { content: 'Fix the bug', status: 'completed' },
        { content: 'Add tests', status: 'in_progress' },
        { content: 'Update docs', status: 'pending' },
      ]),
    ])

    const checklist = await waitFor(() => {
      const el = document.querySelector('.feed-todos')
      if (!el) throw new Error('not rendered yet')
      return el
    })
    expect(checklist.textContent).toContain('1/3')
    expect(checklist.textContent).toContain('Add tests') // the in-progress item, shown collapsed
    expect(checklist.textContent).not.toContain('Old plan item')
  })

  it('ignores a sidechain TodoWrite — a sub-agent scratch list is not the plan', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      todoWrite('t1', [{ content: "Sub-agent's own task", status: 'pending' }], { sidechain: true }),
    ])

    await screen.findByText('TodoWrite')
    expect(document.querySelector('.feed-todos')).toBeNull()
  })

  it('renders nothing when there is no TodoWrite call yet', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [ev({ id: 'a1', kind: 'assistant', text: 'hello' })])

    await screen.findByText('hello')
    expect(document.querySelector('.feed-todos')).toBeNull()
  })

  it('expands to show every item, styled by status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)
    render(<Feed projectId="p1" session="s1" running />)

    pushPage(await nextSocket(), [
      todoWrite('t1', [
        { content: 'Fix the bug', status: 'completed' },
        { content: 'Add tests', status: 'in_progress' },
        { content: 'Update docs', status: 'pending' },
      ]),
    ])

    const head = await waitFor(() => {
      const el = document.querySelector('.feed-todos-head')
      if (!el) throw new Error('not rendered yet')
      return el as HTMLElement
    })
    fireEvent.click(head)

    const items = document.querySelectorAll('.feed-todo-item')
    expect(items).toHaveLength(3)
    expect(items[0].className).toContain('done')
    expect(items[1].className).toContain('active')
    expect(items[2].className).not.toContain('done')
    expect(items[2].className).not.toContain('active')
  })
})
