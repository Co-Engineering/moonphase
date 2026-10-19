import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Settings } from '../../routes/Settings'
import { AccountDialog, SessionAccountSwitcher } from '../AccountPicker'
import {
  api,
  type HarnessAccount,
  type HarnessLogin,
  type Project,
  type Session,
  type WorkspaceProfile,
} from '../../lib/api'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

vi.mock('../../lib/notifications', () => ({
  pushSupport: () => ({ supported: false, reason: 'not in a test' }),
  isInstalled: () => false,
  isApplePhone: () => false,
  enable: async () => ({}),
  disable: async () => ({}),
}))

afterEach(() => vi.restoreAllMocks())

const profile = {
  harness_connected: true,
  harness_auth_mode: 'oauth',
  github_connected: false,
} as unknown as WorkspaceProfile

const account = (over: Partial<HarnessAccount>): HarnessAccount => ({
  id: 'a1',
  label: 'Personal',
  auth_mode: 'oauth',
  is_default: true,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
})

const personal = account({})
const work = account({ id: 'a2', label: 'Work', is_default: false })

function settingsWith(accounts: HarnessAccount[]) {
  vi.spyOn(api, 'profile').mockResolvedValue(profile)
  vi.spyOn(api, 'environments').mockResolvedValue([])
  vi.spyOn(api, 'harnessAccounts').mockResolvedValue(accounts)
  render(<Settings onClose={() => {}} onSaved={() => {}} />)
}

describe('Claude accounts in Settings', () => {
  it('lists every account and marks the default', async () => {
    settingsWith([personal, work])
    expect(await screen.findByText('Personal')).toBeTruthy()
    expect(screen.getByText('Work')).toBeTruthy()
    expect(screen.getByText('default')).toBeTruthy()
    // Only the one that is not already the default offers to become it.
    expect(screen.getAllByRole('button', { name: 'Make default' })).toHaveLength(1)
  })

  it('adds another account under the name given', async () => {
    settingsWith([personal])
    const start = vi
      .spyOn(api, 'startHarnessLogin')
      .mockResolvedValue({ session_id: 's', state: 'awaiting_code', url: 'https://x', detail: null, pane: null } as HarnessLogin)

    fireEvent.click(await screen.findByRole('button', { name: 'Add another account' }))
    fireEvent.change(screen.getByPlaceholderText(/Work, a client/), {
      target: { value: 'Client X' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Claude' }))

    await waitFor(() => expect(start).toHaveBeenCalledWith({ label: 'Client X' }))
  })

  it('signs an existing account in again rather than adding one', async () => {
    settingsWith([personal, work])
    const start = vi
      .spyOn(api, 'startHarnessLogin')
      .mockResolvedValue({ session_id: 's', state: 'awaiting_code', url: 'https://x', detail: null, pane: null } as HarnessLogin)

    const again = await screen.findAllByRole('button', { name: 'Sign in again' })
    fireEvent.click(again[1])
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Claude' }))

    await waitFor(() => expect(start).toHaveBeenCalledWith({ accountId: 'a2' }))
  })

  it('asks before removing one', async () => {
    settingsWith([personal, work])
    const remove = vi.spyOn(api, 'deleteHarnessAccount').mockResolvedValue([personal])

    const buttons = await screen.findAllByRole('button', { name: 'Remove' })
    fireEvent.click(buttons[1])
    expect(remove).not.toHaveBeenCalled()
    const confirmation = screen.getByText(/Remove it\?/).parentElement as HTMLElement
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(remove).toHaveBeenCalledWith('a2'))
  })
})

describe('switching a session to another account', () => {
  const project = { id: 'p1', harness_credential_id: null } as unknown as Project
  const session = {
    tmux_session: 'olol',
    harness_credential_id: null,
  } as unknown as Session

  it('confirms before restarting, then switches', async () => {
    const switched = vi
      .spyOn(api, 'switchSessionAccount')
      .mockResolvedValue(session)
    const onSwitched = vi.fn()
    render(
      <SessionAccountSwitcher
        project={project}
        session={session}
        accounts={[personal, work]}
        onSwitched={onSwitched}
      />,
    )

    fireEvent.change(screen.getByLabelText('Claude account'), { target: { value: 'a2' } })
    expect(switched).not.toHaveBeenCalled()
    expect(screen.getByText(/Restart on Work\?/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(switched).toHaveBeenCalledWith('p1', 'olol', 'a2'))
    await waitFor(() => expect(onSwitched).toHaveBeenCalled())
  })

  it('cancelling leaves the session alone', async () => {
    const switched = vi.spyOn(api, 'switchSessionAccount')
    render(
      <SessionAccountSwitcher
        project={project}
        session={session}
        accounts={[personal, work]}
        onSwitched={() => {}}
      />,
    )
    fireEvent.change(screen.getByLabelText('Claude account'), { target: { value: 'a2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(switched).not.toHaveBeenCalled()
    expect(screen.queryByText(/Restart on/)).toBeNull()
  })
})


describe('the account dialog the sidebar menus open', () => {
  const project = { id: 'p1', name: 'Clockwork', harness_credential_id: null } as unknown as Project
  const session = { tmux_session: 'olol', display_name: null, harness_credential_id: null } as unknown as Session

  it('sets the project account', async () => {
    const set = vi.spyOn(api, 'setProjectAccount').mockResolvedValue(project)
    render(
      <AccountDialog project={project} accounts={[personal, work]} onChanged={() => {}} onClose={() => {}} />,
    )
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'a2' } })
    await waitFor(() => expect(set).toHaveBeenCalledWith('p1', 'a2'))
  })

  it('switches a session, then closes', async () => {
    vi.spyOn(api, 'switchSessionAccount').mockResolvedValue(session)
    const onClose = vi.fn()
    render(
      <AccountDialog
        project={project}
        session={session}
        accounts={[personal, work]}
        onChanged={() => {}}
        onClose={onClose}
      />,
    )
    fireEvent.change(screen.getByLabelText('Claude account'), { target: { value: 'a2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('explains how to get a choice with only one account', () => {
    render(
      <AccountDialog project={project} accounts={[personal]} onChanged={() => {}} onClose={() => {}} />,
    )
    expect(screen.getByText(/Add another under/)).toBeTruthy()
  })
})
