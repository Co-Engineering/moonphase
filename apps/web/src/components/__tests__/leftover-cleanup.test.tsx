import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LeftoverCleanup } from '../LeftoverCleanup'

vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

afterEach(() => vi.unstubAllGlobals())

function stubApi(found: unknown) {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null })
      if (method === 'POST') return json({ removed: ['old-1', 'old-2'], freed_bytes: 5 * 1024 ** 3 })
      return json(found)
    }),
  )
  return calls
}

describe('LeftoverCleanup', () => {
  it('shows what was found before deleting anything', async () => {
    const calls = stubApi({
      sessions: [
        { name: 'old-1', bytes: 4 * 1024 ** 3, modified_at: '2026-09-01T00:00:00Z' },
        { name: 'old-2', bytes: 1024 ** 3, modified_at: '2026-09-02T00:00:00Z' },
      ],
      total_bytes: 5 * 1024 ** 3,
    })
    render(<LeftoverCleanup projectId="p1" onClose={() => {}} />)

    await screen.findByText('old-1')
    expect(screen.getByText(/2 folders from deleted sessions/)).toBeTruthy()
    expect(calls.every((c) => c.method === 'GET')).toBe(true)
  })

  it('deletes exactly the folders it showed, and reports what was freed', async () => {
    const calls = stubApi({
      sessions: [
        { name: 'old-1', bytes: 4 * 1024 ** 3, modified_at: '2026-09-01T00:00:00Z' },
        { name: 'old-2', bytes: 1024 ** 3, modified_at: '2026-09-02T00:00:00Z' },
      ],
      total_bytes: 5 * 1024 ** 3,
    })
    render(<LeftoverCleanup projectId="p1" onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete 2 folders' }))

    await screen.findByText(/^Freed/)
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.url).toContain('/api/projects/p1/leftovers/clean')
    expect(post?.body).toEqual({ names: ['old-1', 'old-2'] })
  })

  it('says so when there is nothing to clean', async () => {
    stubApi({ sessions: [], total_bytes: 0 })
    render(<LeftoverCleanup projectId="p1" onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Nothing left over/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Delete/ })).toBeNull()
  })
})
