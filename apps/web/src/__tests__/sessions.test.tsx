import { useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { fireEvent } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectView } from '../App'
import { api, type Project, type Session } from '../lib/api'

vi.mock('../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

// jsdom has no media queries, and the view defaults by screen width.
vi.stubGlobal(
  'matchMedia',
  (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  }),
)

afterEach(() => vi.restoreAllMocks())

const project = {
  id: 'p1',
  name: 'hello_world',
  status: 'running',
  access: 'admin',
  server_name: 'test',
  environment: 'debian',
  share_count: 0,
  status_detail: null,
} as unknown as Project

const session = (over: Partial<Session>): Session =>
  ({
    id: over.tmux_session ?? 's1',
    tmux_session: 'olol',
    is_mine: true,
    state: 'running',
    owner: 'olol@example.test',
    branch: 'moonphase/olol',
    ...over,
  }) as unknown as Session

const view = (sessions: Session[]) =>
  render(
    <ProjectView
      project={project}
      session={null}
      sessions={sessions}
      onEnter={() => {}}
      onChanged={() => {}}
      onToggleSidebar={() => {}}
      onShare={() => {}}
      onRemoved={() => {}}
    />,
  )

/**
 * A session is a whole agent — its own home, its own worktree, its own branch —
 * so two in one project is the ordinary way to have one refactoring while
 * another chases a bug. The API has always allowed it. The button that makes
 * one disappeared as soon as you had one, so one per project looked like the
 * rule.
 */
describe('starting sessions', () => {
  it('still offers a new one when you already have some', async () => {
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'olol-2' }),
    )
    view([session({})])

    fireEvent.click(await screen.findByRole('button', { name: 'New session' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
  })

  it('names it for you when you do not care to', async () => {
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(session({}))
    view([])

    fireEvent.click(await screen.findByRole('button', { name: 'Start my session' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('p1', undefined, undefined, undefined))
  })

  it('passes the name you typed', async () => {
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'refactor' }),
    )
    view([session({})])

    fireEvent.change(screen.getByLabelText('New session name'), {
      target: { value: 'refactor' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'New session' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('p1', 'refactor', undefined, undefined))
  })

  it('also takes a name for the very first session, not just later ones', async () => {
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'refactor' }),
    )
    view([])

    fireEvent.change(await screen.findByLabelText('New session name'), {
      target: { value: 'refactor' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start my session' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('p1', 'refactor', undefined, undefined))
  })

  it('lets you pick a starting branch', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main', 'staging'])
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'refactor' }),
    )
    view([session({})])

    const select = await screen.findByLabelText<HTMLSelectElement>('Starting branch')
    await waitFor(() => expect(select.options.length).toBe(2))

    fireEvent.change(select, { target: { value: 'staging' } })
    fireEvent.click(screen.getByRole('button', { name: 'New session' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith('p1', undefined, 'staging', undefined),
    )
  })

  it('offers no account choice with only one account', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    view([session({})])
    await screen.findByLabelText('Starting branch')
    expect(screen.queryByLabelText('Claude account')).toBeNull()
  })

  it('starts a session on the account you pick', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    const create = vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'client' }),
    )
    const account = (id: string, label: string, is_default: boolean) => ({
      id,
      label,
      auth_mode: 'oauth' as const,
      is_default,
      created_at: '',
      updated_at: '',
    })
    render(
      <ProjectView
        project={{ ...project, harness_credential_id: null }}
        session={null}
        sessions={[]}
        accounts={[account('a1', 'Personal', true), account('a2', 'Client X', false)]}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )

    const picker = screen
      .getAllByLabelText<HTMLSelectElement>('Claude account')
      .find((el) => el.closest('.new-session'))!
    fireEvent.change(picker, { target: { value: 'a2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start my session' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith('p1', undefined, undefined, 'a2'),
    )
  })
})

/**
 * `tmux_session` names the actual tmux session and can never safely change —
 * it derives the worktree and branch — so `display_name` is the editable
 * label wherever a session's name is shown, falling back to tmux_session
 * until someone sets one.
 */
describe('a session with a display name', () => {
  it('shows the display name instead of the tmux session name', async () => {
    view([session({ tmux_session: 'olol', display_name: 'My cool session' })])

    expect(await screen.findByText('My cool session')).toBeTruthy()
    expect(screen.queryByText('olol')).toBeNull()
  })

  it('falls back to the tmux session name until one is set', async () => {
    view([session({ tmux_session: 'olol', display_name: null })])

    expect(await screen.findByText('olol')).toBeTruthy()
  })
})

/**
 * The other half of being able to start several. The endpoint has always
 * existed and nothing in the app called it, so a list that only grew was the
 * only list you could have.
 */
describe('closing a session', () => {
  it('offers it on your own sessions', async () => {
    const remove = vi.spyOn(api, 'deleteSession').mockResolvedValue(undefined)
    view([session({})])

    fireEvent.click(await screen.findByRole('button', { name: 'close' }))

    await waitFor(() => expect(remove).toHaveBeenCalledWith('p1', 'olol'))
  })

  it('offers it on a colleague\'s only to an owner', () => {
    const theirs = session({ tmux_session: 'sam', is_mine: false, owner: 'sam@x.test' })

    const asOwner = view([theirs])
    expect(screen.queryByRole('button', { name: 'close' })).toBeTruthy()
    asOwner.unmount()

    render(
      <ProjectView
        project={{ ...project, access: 'member' } as unknown as Project}
        session={null}
        sessions={[theirs]}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )
    expect(screen.queryByRole('button', { name: 'close' })).toBeNull()
  })
})

/**
 * A session starts from origin's tip of its branch, refreshed on the spot.
 * When that could not happen as asked — origin unreachable, local commits
 * that win over origin's, a requested branch ignored because the session's
 * own already existed — the server says so once, in the create response, and
 * it has to reach the person rather than vanish with the response.
 */
describe('where a new session started', () => {
  const note =
    'Could not refresh main from origin (could not resolve host), so this session starts ' +
    'from the copy already here, which may be behind.'

  function WithNotes() {
    const [startNote, setStartNote] = useState<string | null>(null)
    return (
      <ProjectView
        project={project}
        session={null}
        sessions={[session({})]}
        startNote={startNote}
        onStartNote={setStartNote}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />
    )
  }

  it('is shown as information, not as an error', async () => {
    vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'olol-2', start_note: note }),
    )
    render(<WithNotes />)

    fireEvent.click(await screen.findByRole('button', { name: 'New session' }))

    const banner = (await screen.findByText(note)).closest('.banner')
    expect(banner?.classList.contains('info')).toBe(true)
    expect(banner?.classList.contains('error')).toBe(false)
  })

  it('can be dismissed', async () => {
    vi.spyOn(api, 'createSession').mockResolvedValue(
      session({ tmux_session: 'olol-2', start_note: note }),
    )
    render(<WithNotes />)
    fireEvent.click(await screen.findByRole('button', { name: 'New session' }))
    await screen.findByText(note)

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    expect(screen.queryByText(note)).toBeNull()
  })

  it('shows nothing when there was nothing to say', async () => {
    const create = vi
      .spyOn(api, 'createSession')
      .mockResolvedValue(session({ tmux_session: 'olol-2' }))
    render(<WithNotes />)

    fireEvent.click(await screen.findByRole('button', { name: 'New session' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(screen.queryByRole('status')).toBeNull()
  })
})
