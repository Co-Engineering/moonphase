import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Settings } from '../../routes/Settings'
import {
  api,
  type HarnessAccount,
  type HarnessInfo,
  type HarnessLogin,
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
  harness_connected: false,
  harness_auth_mode: null,
  github_connected: false,
} as unknown as WorkspaceProfile

const info = (over: Partial<HarnessInfo>): HarnessInfo => ({
  kind: 'claude_code',
  display_name: 'Claude Code',
  supported_auth_modes: ['oauth', 'api_key'],
  available: true,
  configured: false,
  login_supported: true,
  ...over,
})

const claude = info({})
const antigravity = info({ kind: 'antigravity', display_name: 'Antigravity' })

const account = (over: Partial<HarnessAccount>): HarnessAccount => ({
  id: 'a1',
  label: 'Personal',
  auth_mode: 'oauth',
  is_default: true,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
})

const awaitingCode: HarnessLogin = {
  session_id: 's',
  state: 'awaiting_code',
  url: 'https://accounts.google.com/o/oauth2/auth?x=1',
  detail: null,
  pane: null,
}

function settingsWith(
  harnesses: HarnessInfo[],
  accounts: Partial<Record<string, HarnessAccount[]>> = {},
) {
  vi.spyOn(api, 'profile').mockResolvedValue(profile)
  vi.spyOn(api, 'environments').mockResolvedValue([])
  const asked = vi.spyOn(api, 'harnesses').mockResolvedValue(harnesses)
  const listed = vi
    .spyOn(api, 'harnessAccounts')
    .mockImplementation(async (harness = 'claude_code') => accounts[harness] ?? [])
  render(<Settings onClose={() => {}} onSaved={() => {}} />)
  return { asked, listed }
}

/**
 * Settings → Accounts could only ever connect Claude.
 *
 * New project offers a harness only once it is `configured` — signed in — and
 * the only sign-in on this screen was Claude's, calling the API with its
 * default harness. So Antigravity could be installed, registered and listed by
 * the backend and still never be choosable: nothing anywhere could sign in to
 * it. Its card has to start *Antigravity's* sign-in and store *its* key, and
 * offer only what the harness says it supports.
 */
describe('the Antigravity account card', () => {
  it('starts a Google sign-in for Antigravity, not Claude', async () => {
    settingsWith([claude, antigravity])
    const start = vi.spyOn(api, 'startHarnessLogin').mockResolvedValue(awaitingCode)

    fireEvent.click(
      await screen.findByRole('button', { name: 'Sign in with Google' }, { timeout: 6000 }),
    )

    await waitFor(() => expect(start).toHaveBeenCalledWith({ label: 'Personal' }, 'antigravity'))
    // The relay is the same open-a-link, paste-a-code flow Claude's is.
    expect(
      await screen.findByText('https://accounts.google.com/o/oauth2/auth?x=1'),
    ).toBeTruthy()
    expect(screen.getByPlaceholderText('Authorization code')).toBeTruthy()
  })

  it('stores a Gemini API key against Antigravity', async () => {
    settingsWith([claude, antigravity])
    const save = vi.spyOn(api, 'setHarnessApiKey').mockResolvedValue(profile)

    fireEvent.click(
      await screen.findByRole(
        'button',
        { name: 'Use a Gemini API key instead' },
        { timeout: 6000 },
      ),
    )
    const field = screen.getByLabelText('Gemini API key')
    expect(field).toHaveAttribute('placeholder', 'AIza…')
    fireEvent.change(field, { target: { value: 'AIza-test-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }))

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith('AIza-test-key', { label: 'Personal' }, 'antigravity'),
    )
  })

  it('sits alongside Claude’s, which keeps every word it had', async () => {
    settingsWith([claude, antigravity])

    expect(
      await screen.findByRole('button', { name: 'Sign in with Claude' }, { timeout: 6000 }),
    ).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Use an API key instead' })).toBeTruthy()
    expect(await screen.findByRole('heading', { name: 'Antigravity' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Claude' })).toBeTruthy()
  })

  it('is not there when the instance does not run Antigravity', async () => {
    const { asked, listed } = settingsWith([claude])

    await screen.findByRole('button', { name: 'Sign in with Claude' }, { timeout: 6000 })
    // Absent because the answer said so, not because it had yet to arrive.
    await waitFor(() => expect(asked).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByRole('button', { name: 'Sign in with Google' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Antigravity' })).toBeNull()
    expect(listed).not.toHaveBeenCalledWith('antigravity')
  })

  it('offers only the key when the harness has no sign-in to relay', async () => {
    settingsWith([claude, { ...antigravity, login_supported: false }])

    // The key is the whole of it: shown straight away, not behind a button.
    expect(await screen.findByLabelText('Gemini API key')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Sign in with Google' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use a Gemini API key instead' })).toBeNull()
  })

  it('offers no key when the harness does not take one', async () => {
    settingsWith([claude, { ...antigravity, supported_auth_modes: ['oauth'] }])

    expect(
      await screen.findByRole('button', { name: 'Sign in with Google' }, { timeout: 6000 }),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Use a Gemini API key instead' })).toBeNull()
  })

  it('lists its own accounts, an unnamed sign-in being a Google account', async () => {
    const { listed } = settingsWith([claude, antigravity], {
      claude_code: [account({ id: 'c1', label: 'Work' })],
      antigravity: [account({ id: 'g1', label: null })],
    })

    expect((await screen.findAllByText('Google account')).length).toBeGreaterThan(0)
    expect(screen.getByText('Work')).toBeTruthy()
    // Claude's card still calls its own unnamed sign-in what it always did.
    expect(screen.getByText('Claude subscription')).toBeTruthy()
    expect(listed).toHaveBeenCalledWith('claude_code')
    expect(listed).toHaveBeenCalledWith('antigravity')
  })
})

/**
 * agy asks Google's terms and folder trust in every new session, and both are
 * the user's to answer. Moonphase answers them only once told to — asked as
 * the first Google account is added, off until then, and changeable after.
 */
describe('agy’s first-run questions', () => {
  it('asks before the first Google account is added, with both off', async () => {
    settingsWith([claude, antigravity])

    const terms = await screen.findByRole('checkbox', { name: /accept google’s terms/i })
    const trust = screen.getByRole('checkbox', { name: /trust each session’s folder/i })
    expect(terms).not.toBeChecked()
    expect(trust).not.toBeChecked()
    expect(screen.getByText(/before you add it/i)).toBeInTheDocument()
  })

  it('saves each choice on its own, leaving the other as it was', async () => {
    settingsWith([claude, antigravity])
    const saved = vi
      .spyOn(api, 'setAntigravityFirstRun')
      .mockResolvedValue({ ...profile, antigravity_accept_terms: true })

    fireEvent.click(await screen.findByRole('checkbox', { name: /accept google’s terms/i }))

    await waitFor(() =>
      expect(saved).toHaveBeenCalledWith({ accept_terms: true, trust_folders: false }),
    )
  })

  it('stays on the card to change once there is an account', async () => {
    vi.spyOn(api, 'profile').mockResolvedValue({
      ...profile,
      antigravity_accept_terms: true,
    } as WorkspaceProfile)
    vi.spyOn(api, 'environments').mockResolvedValue([])
    vi.spyOn(api, 'harnesses').mockResolvedValue([claude, antigravity])
    vi.spyOn(api, 'harnessAccounts').mockImplementation(async (harness = 'claude_code') =>
      harness === 'antigravity' ? [account({})] : [],
    )
    render(<Settings onClose={() => {}} onSaved={() => {}} />)

    expect(
      await screen.findByRole('checkbox', { name: /accept google’s terms/i }),
    ).toBeChecked()
    expect(screen.getByText(/in each new session/i)).toBeInTheDocument()
    // Claude's card has no such questions.
    expect(screen.getAllByRole('checkbox', { name: /accept google’s terms/i })).toHaveLength(1)
  })
})

/**
 * agy's settings had nowhere to live: the one settings tab was Claude Code's,
 * and it was not called that. Each agent now has a tab by its own name.
 */
describe('Antigravity’s own settings', () => {
  const openTab = async () => {
    vi.spyOn(api, 'mcpOAuthConnections').mockResolvedValue([])
    settingsWith([claude, antigravity])
    fireEvent.click(await screen.findByRole('button', { name: 'Antigravity' }))
  }

  it('has a tab of its own, next to Claude Code’s', async () => {
    settingsWith([claude, antigravity])
    expect(await screen.findByRole('button', { name: 'Claude Code' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Antigravity' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Agent settings' })).toBeNull()
  })

  it('saves the mode and the permission prompts every session starts with', async () => {
    const saved = vi
      .spyOn(api, 'setAntigravitySettings')
      .mockResolvedValue({ ...profile, antigravity_settings: { agentMode: 'auto' } })
    await openTab()

    fireEvent.change(await screen.findByRole('combobox', { name: /mode/i }), {
      target: { value: 'auto' },
    })
    fireEvent.change(screen.getByRole('combobox', { name: /permission prompts/i }), {
      target: { value: 'always-proceed' },
    })
    expect(screen.getByText(/the only boundary left/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /save antigravity settings/i }))

    await waitFor(() =>
      expect(saved).toHaveBeenCalledWith({ agentMode: 'auto', toolPermission: 'always-proceed' }),
    )
  })

  it('says what is shared with Claude Code, and leaves out Claude’s own settings', async () => {
    await openTab()

    expect(await screen.findByText(/shared with claude code/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /permissions & behaviour/i })).toBeNull()
  })
})
