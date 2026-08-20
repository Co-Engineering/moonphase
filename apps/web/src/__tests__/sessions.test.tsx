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

    await waitFor(() => expect(create).toHaveBeenCalledWith('p1', undefined))
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

    await waitFor(() => expect(create).toHaveBeenCalledWith('p1', 'refactor'))
  })
})
