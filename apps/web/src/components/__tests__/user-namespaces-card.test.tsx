import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { UserNamespacesCard } from '../UserNamespacesCard'
import { api, type Server } from '../../lib/api'

afterEach(() => vi.restoreAllMocks())

const server = { id: 'srv1', status: 'online', access: 'admin' } as unknown as Server

/**
 * Auto Mode needs user namespaces, which only the host can allow, so the
 * server's owner is asked here — and nothing is said on a server that does
 * not restrict them.
 */
describe('the Antigravity Auto Mode card', () => {
  it('says nothing on a server whose kernel does not restrict them', async () => {
    const asked = vi
      .spyOn(api, 'userNamespaces')
      .mockResolvedValue({ restricted: false, lifted_by_moonphase: false })
    const { container } = render(<UserNamespacesCard server={server} />)

    await waitFor(() => expect(asked).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('lifts the restriction only after a second, explicit click', async () => {
    vi.spyOn(api, 'userNamespaces')
      .mockResolvedValueOnce({ restricted: true, lifted_by_moonphase: false })
      .mockResolvedValue({ restricted: false, lifted_by_moonphase: true })
    const set = vi
      .spyOn(api, 'setUserNamespaces')
      .mockResolvedValue({ restricted: false, lifted_by_moonphase: true })
    render(<UserNamespacesCard server={server} />)

    fireEvent.click(await screen.findByRole('button', { name: /allow auto mode/i }))
    expect(set).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /allow user namespaces/i }))

    await waitFor(() => expect(set).toHaveBeenCalledWith('srv1', true))
    expect(await screen.findByRole('button', { name: /restore the restriction/i })).toBeVisible()
  })

  it('shows why when the server refuses', async () => {
    vi.spyOn(api, 'userNamespaces').mockResolvedValue({
      restricted: true,
      lifted_by_moonphase: false,
    })
    vi.spyOn(api, 'setUserNamespaces').mockRejectedValue(
      new Error('This server does not have passwordless sudo'),
    )
    render(<UserNamespacesCard server={server} />)

    fireEvent.click(await screen.findByRole('button', { name: /allow auto mode/i }))
    fireEvent.click(screen.getByRole('button', { name: /allow user namespaces/i }))

    expect(await screen.findByText(/passwordless sudo/i)).toBeInTheDocument()
  })
})
