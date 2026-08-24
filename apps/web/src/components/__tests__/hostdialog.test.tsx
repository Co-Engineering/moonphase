import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { HostDialog } from '../HostDialog'
import * as host from '../../lib/host'

afterEach(() => vi.restoreAllMocks())

/**
 * The Host control used to forget the host and reload on the first click, with
 * no confirmation — next to Settings and Sign out, where a misclick costs
 * nothing everywhere else. People hit it by accident and had to retype their
 * address to get back in.
 */
describe('changing the host', () => {
  it('changes nothing when the address is left alone', async () => {
    vi.spyOn(host, 'currentHost').mockReturnValue('https://moonphase.example.com')
    const remember = vi.spyOn(host, 'rememberHost').mockImplementation(() => {})
    const fetchConfig = vi.spyOn(host, 'fetchConfig')
    const onClose = vi.fn()

    render(<HostDialog onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(remember).not.toHaveBeenCalled()
    expect(fetchConfig).not.toHaveBeenCalled()
  })

  it('changes nothing when the address is emptied', async () => {
    vi.spyOn(host, 'currentHost').mockReturnValue('https://moonphase.example.com')
    const remember = vi.spyOn(host, 'rememberHost').mockImplementation(() => {})
    const onClose = vi.fn()

    render(<HostDialog onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Address'), { target: { value: '  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(remember).not.toHaveBeenCalled()
  })

  it('checks a new address before giving up the old one', async () => {
    vi.spyOn(host, 'currentHost').mockReturnValue('https://old.example.com')
    const remember = vi.spyOn(host, 'rememberHost').mockImplementation(() => {})
    vi.spyOn(host, 'fetchConfig').mockRejectedValue(new Error('not a Moonphase host'))

    render(<HostDialog onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Address'), {
      target: { value: 'https://typo.example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // The error is shown and the old host is still what the app uses: a typo
    // must not be able to strand somebody on a screen with no way back.
    expect(await screen.findByText(/not a Moonphase host/)).toBeTruthy()
    expect(remember).not.toHaveBeenCalled()
  })

  it('cancelling changes nothing', () => {
    vi.spyOn(host, 'currentHost').mockReturnValue('https://moonphase.example.com')
    const remember = vi.spyOn(host, 'rememberHost').mockImplementation(() => {})
    const onClose = vi.fn()

    render(<HostDialog onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Address'), {
      target: { value: 'https://somewhere-else.example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onClose).toHaveBeenCalled()
    expect(remember).not.toHaveBeenCalled()
  })
})
