import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed, FeedRow } from '../Feed'
import { api, type FeedEvent, type FeedPage } from '../../lib/api'

// jsdom implements neither; the feed scrolls to the bottom on every batch.
Element.prototype.scrollIntoView = vi.fn()

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const event = (over: Partial<FeedEvent> = {}): FeedEvent => ({
  id: 'e1',
  kind: 'assistant',
  text: 'Done.',
  at: null,
  tool: null,
  ok: true,
  sidechain: false,
  diff: null,
  added: 0,
  removed: 0,
  truncated: false,
  image_media_type: null,
  image_data: null,
  todos: null,
  ...over,
})

/**
 * Every feed called the agent "Claude" — on each of its rows, and in the
 * compose box while it worked — because Claude Code was the only harness with
 * a feed. An Antigravity session's replies are not Claude's, so the feed
 * takes the agent's name from the project's harness. Claude's own feed must
 * not change by a character.
 */
describe('what the feed calls the agent', () => {
  it('is Claude unless told otherwise', () => {
    render(<FeedRow event={event()} />)
    expect(screen.getByText('Claude')).toBeTruthy()
  })

  it('is the harness’s agent when it is given one', () => {
    render(<FeedRow event={event()} agent="Antigravity" />)
    expect(screen.getByText('Antigravity')).toBeTruthy()
    expect(screen.queryByText('Claude')).toBeNull()
  })

  it('names it on its rows and while it works', async () => {
    // No socket here, so the feed falls back to polling — one page is enough.
    vi.stubGlobal(
      'WebSocket',
      class {
        constructor() {
          throw new Error('no sockets in this test')
        }
      },
    )
    vi.spyOn(api, 'feed').mockResolvedValue({
      events: [event()],
      cursor: '',
      available: true,
      activity: 'working',
      prompt: null,
    } as unknown as FeedPage)

    render(<Feed projectId="p1" session="s1" running agent="Antigravity" />)

    expect(await screen.findByText('Antigravity')).toBeTruthy()
    expect(await screen.findByPlaceholderText('Antigravity is working…')).toBeTruthy()
  })

  it('still says Claude is working in a Claude session', async () => {
    vi.stubGlobal(
      'WebSocket',
      class {
        constructor() {
          throw new Error('no sockets in this test')
        }
      },
    )
    vi.spyOn(api, 'feed').mockResolvedValue({
      events: [event()],
      cursor: '',
      available: true,
      activity: 'working',
      prompt: null,
    } as unknown as FeedPage)

    render(<Feed projectId="p1" session="s1" running />)

    expect(await screen.findByPlaceholderText('Claude is working…')).toBeTruthy()
  })
})
