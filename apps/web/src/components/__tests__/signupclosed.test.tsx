import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Auth } from '../../routes/Auth'
import * as api from '../../lib/api'

vi.mock('../../lib/supabase', () => ({
  client: () => ({ auth: { signInWithPassword: vi.fn(), signUp: vi.fn() } }),
  accessToken: async () => null,
}))

afterEach(() => vi.restoreAllMocks())

const methods = { enabled: ['password'] } as unknown as Awaited<
  ReturnType<typeof api.authMethods>
>

/**
 * Closing signup closed the door and left the sign on it.
 *
 * The link was drawn whatever the setting said, so the only way to find out an
 * instance was not taking accounts was to fill the form in — and what came back
 * was a JSON parse error, because the proxy refused with an empty body.
 */
describe('an instance that is not taking accounts', () => {
  it('does not offer to create one', async () => {
    vi.spyOn(api, 'authMethods').mockResolvedValue(methods)
    vi.spyOn(api, 'signupOpen').mockResolvedValue(false)

    render(<Auth />)

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Create one' })).toBeNull(),
    )
    expect(screen.getByText(/not taking new accounts/i)).toBeTruthy()
  })

  it('still offers it where signup is open', async () => {
    vi.spyOn(api, 'authMethods').mockResolvedValue(methods)
    vi.spyOn(api, 'signupOpen').mockResolvedValue(true)

    render(<Auth />)

    const link = await screen.findByRole('button', { name: 'Create one' })
    fireEvent.click(link)
    expect(await screen.findByRole('button', { name: 'Create account' })).toBeTruthy()
  })

  it('assumes open when the instance cannot say', async () => {
    // An older instance, or one that failed to answer. The server refuses the
    // attempt either way, so guessing open costs nothing and guessing closed
    // would hide the link on an instance that wanted it.
    vi.spyOn(api, 'authMethods').mockResolvedValue(methods)
    vi.spyOn(api, 'signupOpen').mockRejectedValue(new Error('offline'))

    render(<Auth />)

    expect(await screen.findByRole('button', { name: 'Create one' })).toBeTruthy()
  })
})
