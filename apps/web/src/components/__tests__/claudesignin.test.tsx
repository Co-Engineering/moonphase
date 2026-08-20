import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Settings } from '../../routes/Settings'
import { api, type HarnessLogin, type WorkspaceProfile } from '../../lib/api'

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

const login = (over: Partial<HarnessLogin>): HarnessLogin => ({
  session_id: 'sess-1',
  state: 'starting',
  url: null,
  detail: null,
  pane: null,
  ...over,
})

/**
 * Starting a sign-in returns before there is a URL to show.
 *
 * Preparing one builds a container image on the server, which is minutes on a
 * cold machine — too long to hold a request open, as the browser gives up and
 * reports a network error for a sign-in that is working. So the answer to
 * "start" is `starting`, and this is the test that a `starting` answer is not a
 * dead end: without something watching it, the button does nothing at all,
 * which is precisely the symptom the wait was meant to cure.
 */
describe('signing in to Claude', () => {
  it('waits for the URL instead of stopping at the first answer', async () => {
    vi.spyOn(api, 'profile').mockResolvedValue(profile)
    vi.spyOn(api, 'environments').mockResolvedValue([])
    vi.spyOn(api, 'startHarnessLogin').mockResolvedValue(
      login({ detail: 'Preparing the container image. First time only.' }),
    )
    vi.spyOn(api, 'pollHarnessLogin').mockResolvedValue(
      login({ state: 'awaiting_code', url: 'https://claude.com/oauth/authorize?x=1' }),
    )

    render(<Settings onClose={() => {}} onSaved={() => {}} />)

    fireEvent.click(
      await screen.findByRole('button', { name: 'Sign in with Claude' }),
    )

    // What it is doing, while it does it: the slow step is a container build,
    // and a button that says "Starting…" for four minutes looks stuck.
    expect(
      await screen.findByText('Preparing the container image. First time only.'),
    ).toBeTruthy()

    await waitFor(
      () =>
        expect(
          screen.getByText('https://claude.com/oauth/authorize?x=1'),
        ).toBeTruthy(),
      { timeout: 6000 },
    )
  })

  it('says so when the preparation fails, rather than going quiet', async () => {
    vi.spyOn(api, 'profile').mockResolvedValue(profile)
    vi.spyOn(api, 'environments').mockResolvedValue([])
    vi.spyOn(api, 'startHarnessLogin').mockResolvedValue(login({}))
    vi.spyOn(api, 'pollHarnessLogin').mockResolvedValue(
      login({ state: 'error', detail: 'Could not build the environment image.' }),
    )

    render(<Settings onClose={() => {}} onSaved={() => {}} />)
    fireEvent.click(
      await screen.findByRole('button', { name: 'Sign in with Claude' }),
    )

    await waitFor(
      () =>
        expect(
          screen.getByText('Could not build the environment image.'),
        ).toBeTruthy(),
      { timeout: 6000 },
    )
  })
})
