import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Feed } from '../Feed'

// jsdom implements neither of these. scrollIntoView runs on every event batch
// to keep the reader pinned to the bottom; createObjectURL/revokeObjectURL back
// every attachment's thumbnail. Both are incidental to what these tests check.
Element.prototype.scrollIntoView = vi.fn()
URL.createObjectURL = vi.fn(() => 'blob:test')
URL.revokeObjectURL = vi.fn()

// The API layer refuses to call out without a token, which is correct and
// nothing to do with what these tests are checking.
vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

/**
 * The feed opens a live socket the moment it mounts with `running`. These
 * tests care about the compose bar, not the stream, so the socket is a stub
 * that never calls back — the fallback poll it would otherwise trigger is
 * exactly the kind of unrelated network noise these tests should not depend on.
 */
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

function png(name = 'photo.png') {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/png' })
}

/** `size` is a real getter, but reassigning it on the instance is enough to
 * exercise the size guard without actually allocating a 20MB buffer. */
function oversizedPng(name = 'huge.png') {
  const file = png(name)
  Object.defineProperty(file, 'size', { value: 20 * 1024 * 1024 })
  return file
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('attaching an image in the feed', () => {
  it('uploads a picked file and shows a thumbnail while it is in flight', async () => {
    let resolveUpload!: (path: string) => void
    const uploaded = new Promise<string>((resolve) => {
      resolveUpload = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/feed/upload')) {
          const path = await uploaded
          return new Response(JSON.stringify({ path }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    const { container } = render(<Feed projectId="p1" session="s1" running />)

    const fileInput = container.querySelector('.feed-file-input') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [png()] } })

    // Uploading: a thumbnail is up, and Send refuses to fire early.
    expect(container.querySelector('.feed-attachment')).toBeTruthy()
    expect(screen.getByText('Send')).toBeDisabled()

    resolveUpload('/home/dev/sessions/s1/uploads/abc123.png')

    await waitFor(() => expect(screen.getByText('Send')).not.toBeDisabled())
  })

  it('sends the uploaded path ahead of the typed message, then clears the thumbnail', async () => {
    let sentBody: string | null = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/feed/upload')) {
          return new Response(
            JSON.stringify({ path: '/home/dev/sessions/s1/uploads/a.png' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        if (url.includes('/feed/answer')) {
          sentBody = JSON.parse(String(init?.body)).key
          return new Response(null, { status: 204 })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    const { container } = render(<Feed projectId="p1" session="s1" running />)

    const fileInput = container.querySelector('.feed-file-input') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [png()] } })

    await waitFor(() => expect(screen.getByText('Send')).not.toBeDisabled())

    fireEvent.change(screen.getByPlaceholderText('Send a message'), {
      target: { value: 'what do you make of this?' },
    })
    fireEvent.click(screen.getByText('Send'))

    await waitFor(() =>
      expect(sentBody).toBe('/home/dev/sessions/s1/uploads/a.png\nwhat do you make of this?'),
    )
    // The thumbnail strip clears once the message carrying it has gone out.
    expect(container.querySelector('.feed-attachment')).toBeFalsy()
  })

  it('refuses an oversized image immediately, without ever uploading it', async () => {
    const upload = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/feed/upload')) {
          upload()
          return new Response(JSON.stringify({ path: '/x.png' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    const { container } = render(<Feed projectId="p1" session="s1" running />)
    const fileInput = container.querySelector('.feed-file-input') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [oversizedPng()] } })

    // A thumbnail appears, already marked as failed — a slow phone upload
    // was never attempted just to find out the same thing 15MB later.
    expect(container.querySelector('.feed-attachment.error')).toBeTruthy()
    expect(upload).not.toHaveBeenCalled()
  })
})

describe('attaching a non-image file in the feed', () => {
  it('uploads it into the working tree, not the feed image endpoint', async () => {
    const feedUpload = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/feed/upload')) {
          feedUpload()
          return new Response(JSON.stringify({ path: '/wrong/endpoint' }), { status: 200 })
        }
        if (url.includes('/sessions/upload')) {
          return new Response(
            JSON.stringify({ path: '/home/dev/sessions/s1/work/notes.txt' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return emptyPage()
      }),
    )
    vi.stubGlobal('WebSocket', StubSocket)

    const { container } = render(<Feed projectId="p1" session="s1" running />)
    const fileInput = container.querySelector('.feed-file-input') as HTMLInputElement
    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(fileInput, { target: { files: [file] } })

    // No thumbnail — a text file has nothing to preview — but the filename
    // shows up as a chip, and it does not go through the image endpoint.
    expect(container.querySelector('.feed-attachment.file')).toBeTruthy()
    expect(screen.getByText('notes.txt')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('Send')).not.toBeDisabled())
    expect(feedUpload).not.toHaveBeenCalled()
  })
})
