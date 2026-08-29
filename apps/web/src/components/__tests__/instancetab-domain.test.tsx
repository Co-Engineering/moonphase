import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { InstanceTab } from '../InstanceTab'
import { instance, type AuthMethods, type InstanceSettings } from '../../lib/api'
// authMethods/saveAuthMethods are standalone module exports, not members of
// the `api` object — InstanceTab reaches them via `import * as api`, so the
// mock has to go through the same namespace object to actually intercept it.
import * as apiModule from '../../lib/api'

afterEach(() => vi.restoreAllMocks())

const AUTH_METHODS: AuthMethods = {
  enabled: ['password', 'google', 'microsoft'],
  password_enabled: true,
  magic_link_enabled: false,
  google_enabled: true,
  microsoft_enabled: true,
  smtp_host: '',
  smtp_port: 587,
  smtp_user: '',
  smtp_sender: '',
  google_client_id: 'g-cid',
  microsoft_client_id: 'm-cid',
  microsoft_tenant: 'common',
  redirect_uri: 'https://moonphase.example.com/auth/v1/callback',
  problems: [],
}

const NO_DOMAIN: InstanceSettings = { public_url: null, signup_open: false }

/**
 * The server refuses to actually turn Google/Microsoft on without a domain
 * regardless of what is stored (authconfig.render) — this is the frontend
 * half: the checkbox already showed unchecked-and-greyed-out with no domain,
 * but the draft underneath kept saying `true` until this, so saving this
 * screen for an unrelated reason (or reloading) could still submit a
 * provider as enabled that the screen visibly showed as off.
 */
describe('sign-in methods when the domain is missing', () => {
  it('clears google_enabled/microsoft_enabled in the draft, not just the checkbox', async () => {
    vi.spyOn(instance, 'settings').mockResolvedValue(NO_DOMAIN)
    vi.spyOn(apiModule, 'authMethods').mockResolvedValue(AUTH_METHODS)
    const save = vi.spyOn(apiModule, 'saveAuthMethods').mockResolvedValue(AUTH_METHODS)

    render(<InstanceTab busy={false} run={async (fn) => void (await fn())} />)

    await screen.findByText(/ways to sign in/i)
    // Two cards on this tab, each with its own "Save" button — the second
    // one belongs to "Ways to sign in".
    const saveButtons = screen.getAllByRole('button', { name: /^save$/i })
    saveButtons[saveButtons.length - 1].click()

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({ google_enabled: false, microsoft_enabled: false }),
      ),
    )
  })
})
