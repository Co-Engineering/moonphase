import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SavePoints, when } from '../SavePoints'
import { Summary, describe as describeDigest, sentence } from '../Summary'
import { label, primary } from '../YourApp'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

/**
 * The screens written for someone who cannot check them.
 *
 * Each of these makes a claim its reader has no way to verify: that this is
 * what the agent did, that this is where you can get back to, that this button
 * opens the thing you made. Being wrong is worse here than anywhere else in
 * the product, because there is no diff to fall back on.
 */

function serve(handler: (url: string, init?: RequestInit) => unknown) {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({
        url,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : null,
      })
      const body = handler(url, init)
      return new Response(body === undefined ? '' : JSON.stringify(body), {
        status: body === undefined ? 204 : 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
})

// --- save points -------------------------------------------------------------

const board = {
  unsaved: 0,
  detail: null,
  points: [
    {
      id: 'a'.repeat(40),
      at: new Date(Date.now() - 3600_000).toISOString(),
      label: 'Working login',
      current: true,
      automatic: false,
    },
    {
      id: 'b'.repeat(40),
      at: new Date(Date.now() - 86_400_000).toISOString(),
      label: 'Before the redesign',
      current: false,
      automatic: false,
    },
  ],
}

describe('SavePoints', () => {
  it('says where you are without showing a hash', async () => {
    serve(() => board)
    const { container } = render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText('you are here')).toBeTruthy())
    expect(screen.getByText('Working login')).toBeTruthy()
    // A commit id is not a thing anyone here can use.
    expect(container.textContent).not.toContain('aaaaaaa')
  })

  it('counts unsaved work as a fact rather than a hint', async () => {
    serve(() => ({ ...board, unsaved: 3, points: [{ ...board.points[0], current: false }] }))
    render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText(/files changed since/)).toBeTruthy())
    expect(screen.queryByText('you are here')).toBeNull()
  })

  it('promises the undo has an undo before going back', async () => {
    serve(() => board)
    render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText('Go back to this')).toBeTruthy())
    fireEvent.click(screen.getByText('Go back to this'))

    expect(screen.getByText(/Go back to “Before the redesign”\?/)).toBeTruthy()
    expect(screen.getByText(/come straight back/)).toBeTruthy()
    // The thing they will actually worry about.
    expect(screen.getByText(/Installed packages are left alone/)).toBeTruthy()
  })

  it('does not go back until it is confirmed', async () => {
    const calls = serve((url) => (url.includes('/restore') ? board : board))
    render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText('Go back to this')).toBeTruthy())
    fireEvent.click(screen.getByText('Go back to this'))
    fireEvent.click(screen.getByText('Cancel'))

    expect(calls.filter((c) => c.url.includes('/restore'))).toHaveLength(0)
  })

  it('restores the point that was asked for', async () => {
    const calls = serve(() => board)
    render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText('Go back to this')).toBeTruthy())
    fireEvent.click(screen.getByText('Go back to this'))
    fireEvent.click(screen.getByText('Go back'))

    await waitFor(() => {
      const call = calls.find((c) => c.url.includes('/restore'))
      expect(call?.url).toContain(`checkpoints/${'b'.repeat(40)}/restore`)
    })
  })

  it('sends the name someone typed', async () => {
    const calls = serve(() => board)
    render(<SavePoints projectId="p1" session="s" />)

    await waitFor(() => expect(screen.getByText('Save this version')).toBeTruthy())
    fireEvent.click(screen.getByText('Save this version'))
    fireEvent.change(screen.getByPlaceholderText('What works right now?'), {
      target: { value: 'Login works' },
    })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => {
      const call = calls.find((c) => c.method === 'POST')
      expect(call?.body).toEqual({ label: 'Login works' })
    })
  })
})

describe('when', () => {
  const now = new Date('2026-08-17T12:00:00Z')

  it('speaks in units a person thinks in', () => {
    expect(when('2026-08-17T11:58:00Z', now)).toBe('2 min ago')
    expect(when('2026-08-17T09:00:00Z', now)).toBe('3 hours ago')
    expect(when('2026-08-16T12:00:00Z', now)).toBe('1 day ago')
  })

  it('says nothing rather than "Invalid Date"', () => {
    expect(when('not a date', now)).toBe('')
  })
})

// --- summary -----------------------------------------------------------------

const empty = {
  created: [],
  edited: [],
  commands: 0,
  installs: 0,
  tests: 0,
  searches: 0,
  last_said: '',
  detail: null,
}

describe('describe', () => {
  it('leaves out what did not happen', () => {
    // "installed 0 packages" reads as a failure rather than as an absence.
    expect(describeDigest({ ...empty, created: ['a.py'] })).toEqual(['made 1 new file'])
  })

  it('gets singular and plural right', () => {
    expect(describeDigest({ ...empty, created: ['a', 'b'], installs: 1 })).toEqual([
      'made 2 new files',
      'installed 1 package',
    ])
  })

  it('does not double-count commands against the work they did', () => {
    // "installed 3 packages and ran 12 commands" describes one thing twice.
    const parts = describeDigest({ ...empty, installs: 3, commands: 12 })
    expect(parts).toEqual(['installed 3 packages'])
  })

  it('falls back to commands when there is nothing else to say', () => {
    expect(describeDigest({ ...empty, commands: 4 })).toEqual(['ran 4 commands'])
  })
})

describe('sentence', () => {
  it('reads the way someone would say it', () => {
    expect(sentence(['made 3 new files', 'installed 2 packages', 'ran the tests'])).toBe(
      'Claude made 3 new files, installed 2 packages and ran the tests.',
    )
    expect(sentence(['made 1 new file'])).toBe('Claude made 1 new file.')
  })
})

describe('Summary', () => {
  it('renders nothing when nothing has happened', async () => {
    serve(() => empty)
    const { container } = render(<Summary projectId="p1" session="s" />)

    await waitFor(() => expect(container.querySelector('.summary')).toBeNull())
  })

  it('leads with the count and offers the detail underneath', async () => {
    serve(() => ({
      ...empty,
      created: ['backend/main.py', 'frontend/App.jsx'],
      installs: 2,
      last_said: 'The todo app is running on port 5173.',
    }))
    render(<Summary projectId="p1" session="s" />)

    await waitFor(() =>
      expect(screen.getByText('Claude made 2 new files and installed 2 packages.')).toBeTruthy(),
    )
    expect(screen.getByText(/The todo app is running/)).toBeTruthy()
    expect(screen.getByText('Which files')).toBeTruthy()
  })
})

// --- your app ----------------------------------------------------------------

describe('label', () => {
  it('prefers what the page calls itself', () => {
    expect(label({ port: 5173, kind: 'page', title: 'Todo App', process: null }, 5173)).toBe(
      'Todo App',
    )
  })

  it('never offers a directory listing as the name of your app', () => {
    // It is HTML and so passes for a page, but nobody built "Index of /".
    expect(
      label({ port: 8080, kind: 'page', title: 'Index of /', process: null }, 8080),
    ).toBe('Your app')
  })

  it('names an API as one rather than as your app', () => {
    expect(label({ port: 8000, kind: 'api', title: null, process: null }, 8000)).toBe(
      'Data service',
    )
  })

  it('falls back to the port only when nothing else is known', () => {
    expect(label(undefined, 5174)).toBe('Something on port 5174')
  })
})

describe('primary', () => {
  it('picks the page over the API however they are numbered', () => {
    // Landing on raw JSON looks like the app is broken.
    const services = [
      { port: 8000, kind: 'api' as const, title: null, process: null },
      { port: 5173, kind: 'page' as const, title: 'Todo App', process: null },
    ]
    expect(primary(services)?.port).toBe(5173)
  })

  it('prefers the page that named itself over one that did not', () => {
    // Both are HTML. The untitled one on the lower port was a placeholder, and
    // the app someone built was next door calling itself "Todo" — landing on
    // the placeholder looks exactly like the app is broken.
    const services = [
      { port: 5173, kind: 'page' as const, title: null, process: null },
      { port: 5174, kind: 'page' as const, title: 'Todo', process: null },
    ]
    expect(primary(services)?.port).toBe(5174)
  })

  it('does not treat a file listing as a name', () => {
    const services = [
      { port: 5173, kind: 'page' as const, title: null, process: null },
      { port: 8080, kind: 'page' as const, title: 'Index of /', process: null },
    ]
    expect(primary(services)?.port).toBe(5173)
  })

  it('offers something rather than nothing before a server has answered', () => {
    const services = [{ port: 3000, kind: 'unknown' as const, title: null, process: null }]
    expect(primary(services)?.port).toBe(3000)
  })

  it('is null when nothing is running', () => {
    expect(primary([])).toBeNull()
  })
})
