import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SignInMethods, draftFrom } from '../SignInMethods'
import { Setup } from '../../routes/Setup'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

/**
 * The sign-in settings, whose failure mode is a button that cannot work.
 *
 * A provider enabled without its credentials sends someone to an error page
 * belonging to Google or Microsoft, with nothing to suggest it was
 * misconfigured here. So what is tested is that the form asks for what each one
 * needs, and never shows a secret back.
 */

afterEach(() => vi.restoreAllMocks())

const empty = draftFrom(null)

describe('draftFrom', () => {
  it('starts with only the method that needs no configuration', () => {
    expect(empty.password_enabled).toBe(true)
    expect(empty.google_enabled).toBe(false)
    expect(empty.microsoft_enabled).toBe(false)
    expect(empty.magic_link_enabled).toBe(false)
  })

  it('never carries a secret in from the server', () => {
    // The API does not return them, and the draft must not invent a value that
    // would overwrite the stored one on the next save.
    const draft = draftFrom({
      enabled: ['google'],
      password_enabled: true,
      magic_link_enabled: false,
      google_enabled: true,
      microsoft_enabled: false,
      smtp_host: '',
      smtp_port: 587,
      smtp_user: '',
      smtp_sender: '',
      google_client_id: 'cid',
      microsoft_client_id: '',
      microsoft_tenant: 'common',
      redirect_uri: 'https://x/auth/v1/callback',
      problems: [],
    })
    expect(draft.google_client_id).toBe('cid')
    expect(draft.google_client_secret).toBe('')
  })
})

describe('SignInMethods', () => {
  it('hides a provider’s fields until it is switched on', () => {
    render(
      <SignInMethods draft={empty} onChange={() => {}} redirectUri="https://x/auth/v1/callback" />,
    )
    expect(screen.getByText('Sign in with Google')).toBeTruthy()
    expect(screen.queryByText(/Redirect URI/)).toBeNull()
  })

  it('shows the redirect URI to paste once it is on', () => {
    render(
      <SignInMethods
        draft={{ ...empty, google_enabled: true }}
        onChange={() => {}}
        redirectUri="https://moonphase.example.com/auth/v1/callback"
      />,
    )
    expect(
      (screen.getByDisplayValue('https://moonphase.example.com/auth/v1/callback') as HTMLInputElement)
        .readOnly,
    ).toBe(true)
  })

  it('says a blank secret keeps the saved one', () => {
    // Otherwise saving any other change would silently erase it.
    render(
      <SignInMethods
        draft={{ ...empty, google_enabled: true }}
        onChange={() => {}}
        redirectUri="https://x/auth/v1/callback"
      />,
    )
    expect(screen.getByPlaceholderText('leave blank to keep the saved one')).toBeTruthy()
  })

  it('refuses to offer Google or Microsoft without a custom domain', () => {
    // Both reject a redirect URI pointing at a bare IP, so a checkbox here
    // would be offering something that cannot work.
    render(
      <SignInMethods
        draft={{ ...empty, google_enabled: true }}
        onChange={() => {}}
        redirectUri="http://203.0.113.10/auth/v1/callback"
        domainMissing
      />,
    )

    const google = screen.getByText('Sign in with Google')
      .closest('label')!
      .querySelector('input') as HTMLInputElement
    expect(google.disabled).toBe(true)
    expect(google.checked).toBe(false)

    const microsoft = screen.getByText('Sign in with Microsoft')
      .closest('label')!
      .querySelector('input') as HTMLInputElement
    expect(microsoft.disabled).toBe(true)

    // Said once for both, rather than the same paragraph under each.
    expect(screen.getAllByText(/need a custom domain/).length).toBe(1)
  })

  it('asks for a mail server before offering magic links', () => {
    render(
      <SignInMethods
        draft={{ ...empty, magic_link_enabled: true }}
        onChange={() => {}}
        redirectUri="https://x/auth/v1/callback"
      />,
    )
    // The fields it cannot work without appear with it, rather than a
    // paragraph explaining that they will be needed later.
    expect(screen.getByPlaceholderText('smtp.example.com')).toBeTruthy()
    expect(screen.getByPlaceholderText('moonphase@example.com')).toBeTruthy()
  })
})


describe('Setup', () => {
  it('does not let other people create accounts unless asked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}', { status: 200 })),
    )
    render(<Setup onDone={() => {}} />)

    // Step one is the account; the toggle lives on step two, and its default
    // is what matters — an instance left open is other people on your machine.
    expect(screen.getByText(/The first account owns this instance/)).toBeTruthy()
    // And it says which of three steps you are on.
    expect(screen.getByLabelText('Step 1 of 3')).toBeTruthy()
    vi.unstubAllGlobals()
  })
})
