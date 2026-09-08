import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'

Element.prototype.scrollIntoView = vi.fn()
URL.createObjectURL = vi.fn(() => 'blob:test')
URL.revokeObjectURL = vi.fn()

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

// The socket is created inside an async connect() (it awaits the auth token
// first), so it does not exist the instant render() returns.
async function nextSocket(): Promise<StubSocket> {
  await waitFor(() => expect(sockets.length).toBeGreaterThan(0))
  return sockets[0]
}

function pushPrompt(
  socket: StubSocket,
  prompt: { question: string; options: { key: string; label: string }[] } | null,
) {
  act(() => {
    socket.onmessage?.({
      data: JSON.stringify({ type: 'prompt', prompt, activity: prompt ? 'awaiting_input' : 'idle' }),
    })
  })
}

/**
 * A numbered permission prompt and the free-text compose box both end up
 * sending raw keystrokes to the same tmux pane (tapping an option sends just
 * its digit — see `send(option.key)` above). Typing free text while a prompt
 * is up can land on the same picker mid-selection depending on what digits
 * it happens to contain, so the compose box gets out of the way entirely
 * while a question is waiting, rather than offering two ways to answer that
 * do not actually behave the same.
 */
describe('the compose box while a permission prompt is showing', () => {
  it('disables typing, attaching and sending until the prompt clears', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)

    render(<Feed projectId="p1" session="s1" running />)
    pushPrompt(await nextSocket(), { question: 'Proceed?', options: [{ key: '1', label: 'Yes' }] })

    const field = await screen.findByPlaceholderText('Tap a choice above to answer')
    expect(field).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(screen.getByLabelText('Attach a file')).toBeDisabled()
  })

  it('re-enables the moment the prompt clears', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => emptyPage()))
    vi.stubGlobal('WebSocket', StubSocket)

    render(<Feed projectId="p1" session="s1" running />)
    const socket = await nextSocket()
    pushPrompt(socket, { question: 'Proceed?', options: [{ key: '1', label: 'Yes' }] })
    await screen.findByPlaceholderText('Tap a choice above to answer')

    pushPrompt(socket, null)

    const field = await screen.findByPlaceholderText('Send a message')
    expect(field).not.toBeDisabled()
  })

  it('still lets you tap a numbered option while typing is disabled', async () => {
    let sentKey: string | null = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/feed/answer')) {
          sentKey = JSON.parse(String(init?.body)).key
          return new Response(null, { status: 204 })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    render(<Feed projectId="p1" session="s1" running />)
    pushPrompt(await nextSocket(), { question: 'Proceed?', options: [{ key: '1', label: 'Yes' }] })

    fireEvent.click(await screen.findByRole('button', { name: /Yes/ }))
    await waitFor(() => expect(sentKey).toBe('1'))
  })
})
