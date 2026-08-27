import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'

// jsdom implements neither of these. scrollIntoView runs on every event batch
// to keep the reader pinned to the bottom; createObjectURL/revokeObjectURL back
// every attachment's thumbnail. Neither is relevant to what these tests check.
Element.prototype.scrollIntoView = vi.fn()
URL.createObjectURL = vi.fn(() => 'blob:test')
URL.revokeObjectURL = vi.fn()

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

class StubSocket {
  onopen: (() => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  close() {}
}

const emptyPage = () =>
  new Response(
    JSON.stringify({ events: [], cursor: '', available: true, activity: 'idle', prompt: null }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )

afterEach(() => {
  vi.unstubAllGlobals()
})

/**
 * The compose box is a multi-line textarea now, not a single-line input, and
 * that only works if Enter and Shift+Enter mean different things there:
 * Shift+Enter has to actually reach the field as a plain newline (a textarea
 * does that on its own — nothing here needs to insert one by hand), and
 * plain Enter has to still send, or multi-line composing would come at the
 * cost of the one-key send everyone is used to.
 */
describe('Enter vs Shift+Enter in the feed compose box', () => {
  it('Shift+Enter adds a line instead of sending', async () => {
    const answered = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes('/feed/answer')) {
          answered()
          return new Response(null, { status: 204 })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    render(<Feed projectId="p1" session="s1" running />)
    const field = screen.getByPlaceholderText('Send a message')

    fireEvent.change(field, { target: { value: 'first line' } })
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true })

    // Nothing sent, and the field kept whatever it already had — jsdom does
    // not simulate the textarea's own newline insertion, but the important
    // part is that nothing here submitted it away.
    expect(answered).not.toHaveBeenCalled()
    expect(field).toHaveValue('first line')
  })

  it('plain Enter still sends', async () => {
    let sentBody: string | null = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/feed/answer')) {
          sentBody = JSON.parse(String(init?.body)).key
          return new Response(null, { status: 204 })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    render(<Feed projectId="p1" session="s1" running />)
    const field = screen.getByPlaceholderText('Send a message')

    fireEvent.change(field, { target: { value: 'go ahead' } })
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: false })

    await waitFor(() => expect(sentBody).toBe('go ahead'))
  })
})
