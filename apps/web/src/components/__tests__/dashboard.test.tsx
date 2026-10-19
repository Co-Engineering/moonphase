import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Dashboard } from '../Dashboard'
import { api, type AccountUsage } from '../../lib/api'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

afterEach(() => vi.restoreAllMocks())

const usage = (over: Partial<AccountUsage>): AccountUsage => ({
  account_id: 'a1',
  label: 'Personal',
  auth_mode: 'oauth',
  is_default: true,
  status: 'ok',
  detail: null,
  windows: [],
  fetched_at: '2026-10-19T10:00:00Z',
  stale: false,
  retry_at: null,
  source: 'stored',
  ...over,
})

describe('the dashboard', () => {
  it('shows every account with its windows and when they reset', async () => {
    vi.spyOn(api, 'accountUsage').mockResolvedValue([
      usage({
        windows: [
          { key: 'five_hour', label: 'Current session', percent: 42.4, resets_at: '2026-10-19T14:00:00Z' },
          { key: 'seven_day', label: 'Current week (all models)', percent: 91, resets_at: '2026-10-22T08:00:00Z' },
        ],
      }),
      usage({ account_id: 'a2', label: 'Client X', is_default: false, auth_mode: 'api_key', status: 'api_key', detail: 'Billed per token, so there are no plan limits to show.' }),
    ])
    render(<Dashboard onToggleSidebar={() => {}} />)

    expect(await screen.findByText('Personal')).toBeTruthy()
    expect(screen.getByText('42% used')).toBeTruthy()
    expect(screen.getByText('91% used')).toBeTruthy()
    expect(screen.getAllByText(/^Resets /)).toHaveLength(2)
    expect(screen.getByText('Client X')).toBeTruthy()
    expect(screen.getByText(/no plan limits/)).toBeTruthy()
  })

  it('says what is wrong, and marks an old reading as out of date', async () => {
    vi.spyOn(api, 'accountUsage').mockResolvedValue([
      usage({
        status: 'expired',
        detail: 'The stored sign-in has expired.',
        stale: true,
        windows: [{ key: 'five_hour', label: 'Current session', percent: 10, resets_at: null }],
      }),
    ])
    render(<Dashboard onToggleSidebar={() => {}} />)
    expect(await screen.findByText(/Out of date — The stored sign-in has expired\./)).toBeTruthy()
  })

  it('says when it will ask again after being told to slow down', async () => {
    vi.spyOn(api, 'accountUsage').mockResolvedValue([
      usage({
        status: 'rate_limited',
        detail: 'Anthropic asked us to slow down.',
        retry_at: '2026-10-19T10:35:00Z',
      }),
    ])
    render(<Dashboard onToggleSidebar={() => {}} />)
    expect(await screen.findByText(/slow down\. Trying again at/)).toBeTruthy()
  })

  it('points to Settings with no accounts', async () => {
    vi.spyOn(api, 'accountUsage').mockResolvedValue([])
    render(<Dashboard onToggleSidebar={() => {}} />)
    expect(await screen.findByText('No Claude accounts yet')).toBeTruthy()
  })
})
