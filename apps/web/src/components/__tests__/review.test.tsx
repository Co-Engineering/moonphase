import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Attention } from '../Attention'
import { Changes, splitPatch } from '../Changes'
import { Search, segments } from '../Search'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

/**
 * The three away-from-the-desk screens.
 *
 * What is worth pinning is not that they render but that they cannot quietly
 * mislead: an answer must go to the session it was shown under, a diff must
 * not attribute one file's changes to another, and a search result must show
 * where the match actually is.
 */

const session = {
  id: 's1',
  project_id: 'p1',
  project_name: 'fresh-demo',
  tmux_session: 'oliver-test',
  is_mine: true,
  activity: 'awaiting_input',
  activity_detail: 'Do you want to proceed?',
  activity_at: '2026-08-17T10:00:00Z',
  checked_at: new Date().toISOString(),
  owner: 'oliver',
  state: 'running',
  harness: 'claude_code',
  started_at: null,
  last_attached_at: null,
  transcript_path: null,
  user_id: 'u1',
}

const question = {
  project_id: 'p1',
  project_name: 'fresh-demo',
  session: 'oliver-test',
  activity_at: '2026-08-17T10:00:00Z',
  question: 'Do you want to proceed?',
  prompt: {
    question: 'Do you want to proceed?',
    options: [
      { key: '1', label: 'Yes' },
      { key: '2', label: 'No, tell Claude what to do differently' },
    ],
  },
  tail: 'Bash(rm -rf build)\nDo you want to proceed?\n 1. Yes\n 2. No',
}

function serve(handler: (url: string, init?: RequestInit) => unknown) {
  const calls: { url: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
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

describe('Attention', () => {
  it('turns a parsed prompt into buttons', async () => {
    serve(() => [question])
    render(<Attention sessions={[session] as never} onOpen={() => {}} />)

    await waitFor(() => expect(screen.getByText('Yes')).toBeTruthy())
    expect(screen.getByText('No, tell Claude what to do differently')).toBeTruthy()
  })

  it('sends the answer to the session it was shown under', async () => {
    const calls = serve((url) => (url.includes('/answer') ? undefined : [question]))
    render(<Attention sessions={[session] as never} onOpen={() => {}} />)

    await waitFor(() => expect(screen.getByText('Yes')).toBeTruthy())
    fireEvent.click(screen.getByText('Yes'))

    await waitFor(() => {
      const answer = calls.find((call) => call.url.includes('/answer'))
      expect(answer).toBeTruthy()
      // The session is named in the path, not implied — answering the wrong
      // one from a list of several is the failure that matters here.
      expect(answer!.url).toContain('/projects/p1/sessions/oliver-test/answer')
      expect(answer!.body).toEqual({ key: '1' })
    })
  })

  it('falls back to a text box when the pane could not be parsed', async () => {
    serve(() => [{ ...question, prompt: null }])
    render(<Attention sessions={[session] as never} onOpen={() => {}} />)

    await waitFor(() => expect(screen.getByPlaceholderText('Type an answer…')).toBeTruthy())
  })

  it('shows what led to the question before you answer it', async () => {
    serve(() => [question])
    render(<Attention sessions={[session] as never} onOpen={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Show the last few lines/)).toBeTruthy())
    fireEvent.click(screen.getByText(/Show the last few lines/))
    expect(screen.getByText(/rm -rf build/)).toBeTruthy()
  })

  it('renders nothing when nothing is waiting', () => {
    serve(() => [])
    const { container } = render(
      <Attention sessions={[{ ...session, activity: 'working' }] as never} onOpen={() => {}} />,
    )

    expect(container.querySelector('.attention')).toBeNull()
  })
})

describe('splitPatch', () => {
  it('keeps each file with its own hunks', () => {
    const patch = [
      'diff --git a/one.py b/one.py',
      '--- a/one.py',
      '+++ b/one.py',
      '@@ -1 +1 @@',
      '-old',
      '+new',
      'diff --git a/two.py b/two.py',
      '@@ -1 +1 @@',
      '+second',
    ].join('\n')

    const out = splitPatch(patch)

    expect(out.get('one.py')).toContain('+new')
    // The bug this guards: one file's hunks appearing under another's name.
    expect(out.get('one.py')).not.toContain('+second')
    expect(out.get('two.py')).toContain('+second')
  })

  it('uses the path as it is now, not as it was', () => {
    const out = splitPatch('diff --git a/old.py b/new.py\n@@ -1 +1 @@\n+x')

    expect([...out.keys()]).toEqual(['new.py'])
  })

  it('is empty for an empty patch rather than throwing', () => {
    expect(splitPatch('').size).toBe(0)
  })
})

describe('Changes', () => {
  const body = {
    branch: 'moonphase/oliver-test',
    base: 'main',
    added: 52,
    removed: 3,
    truncated: false,
    detail: null,
    patch: 'diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new',
    files: [
      { path: 'a.py', added: 52, removed: 3, status: 'modified' },
      { path: 'b.py', added: 0, removed: 0, status: 'untracked' },
    ],
  }

  it('shows the branch, its base and the totals', async () => {
    serve(() => body)
    render(<Changes projectId="p1" session="oliver-test" />)

    await waitFor(() => expect(screen.getByText('moonphase/oliver-test')).toBeTruthy())
    expect(screen.getByText('vs main')).toBeTruthy()
    // Once in the header total and once on the file row.
    expect(screen.getAllByText('+52')).toHaveLength(2)
  })

  it('opens a file to its own patch', async () => {
    serve(() => body)
    render(<Changes projectId="p1" session="oliver-test" />)

    await waitFor(() => expect(screen.getByText('a.py')).toBeTruthy())
    fireEvent.click(screen.getByText('a.py'))
    expect(screen.getByText('+new')).toBeTruthy()
  })

  it('explains that a new file has nothing to compare against', async () => {
    serve(() => body)
    render(<Changes projectId="p1" session="oliver-test" />)

    await waitFor(() => expect(screen.getByText('b.py')).toBeTruthy())
    fireEvent.click(screen.getByText('b.py'))
    expect(screen.getByText(/nothing to compare it against/)).toBeTruthy()
  })

  it('reports a non-repository as a state, not an error', async () => {
    serve(() => ({ ...body, detail: 'not a git repository', files: [] }))
    render(<Changes projectId="p1" session="oliver-test" />)

    await waitFor(() => expect(screen.getByText('not a git repository')).toBeTruthy())
  })
})

describe('search highlighting', () => {
  it('marks every occurrence, case-insensitively', () => {
    const parts = segments('The Rate limiter and the rate limiter', 'rate limiter')

    expect(parts.filter((p) => p.hit).map((p) => p.text)).toEqual([
      'Rate limiter',
      'rate limiter',
    ])
    // Reassembling must give the original back — a highlighter that drops
    // characters is worse than none.
    expect(parts.map((p) => p.text).join('')).toBe('The Rate limiter and the rate limiter')
  })

  it('leaves text alone when there is no match', () => {
    expect(segments('nothing here', 'zzz')).toEqual([{ text: 'nothing here', hit: false }])
  })
})

describe('Search', () => {
  it('does not search until asked', async () => {
    const calls = serve(() => ({ query: 'x', hits: [], partial: false }))
    render(<Search onOpen={() => {}} onClose={() => {}} />)

    fireEvent.change(screen.getByPlaceholderText('A phrase you remember…'), {
      target: { value: 'fastapi' },
    })

    // Each keystroke would be a grep across every container, over SSH.
    expect(calls.filter((c) => c.url.includes('/api/search'))).toHaveLength(0)
  })

  it('shows a hit and opens the session it came from', async () => {
    serve(() => ({
      query: 'fastapi',
      partial: false,
      hits: [
        {
          project_id: 'p1',
          project_name: 'fresh-demo',
          session: 'oliver-test',
          at: '2026-08-16T18:46:00Z',
          role: 'user',
          text: 'Make simple fastapi app with a react frontend',
        },
      ],
    }))
    const opened: string[] = []
    render(<Search onOpen={(p, s) => opened.push(`${p}:${s}`)} onClose={() => {}} />)

    fireEvent.change(screen.getByPlaceholderText('A phrase you remember…'), {
      target: { value: 'fastapi' },
    })
    fireEvent.click(screen.getByText('Search'))

    await waitFor(() => expect(screen.getByText('fastapi')).toBeTruthy())
    fireEvent.click(screen.getByText('fresh-demo'))
    expect(opened).toEqual(['p1:oliver-test'])
  })
})
