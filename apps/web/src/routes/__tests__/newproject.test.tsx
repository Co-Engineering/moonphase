import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NewProject } from '../NewProject'
import { api } from '../../lib/api'

/**
 * Pressing the button twice must not leave two projects behind.
 *
 * Creating a project writes the row and provisions afterwards, so a failure
 * arrives as a status on a project that already exists. The form stays open
 * with the reason on it — which is right, losing what was typed is worse — but
 * the second press called create again. Someone whose private repository would
 * not clone pressed it four times and got four dead projects.
 */

const servers = [
  { id: 'srv-1', name: 'worker', status: 'online' },
] as unknown as Parameters<typeof NewProject>[0]['servers']

const harnesses = [
  { kind: 'claude_code', label: 'Claude Code', available: true, configured: true },
] as unknown as Parameters<typeof NewProject>[0]['harnesses']

const environments = [
  { key: 'debian', label: 'Debian 12', description: 'Stable and small.' },
] as unknown as Parameters<typeof NewProject>[0]['environments']

function renderForm() {
  return render(
    <NewProject
      servers={servers}
      harnesses={harnesses}
      environments={environments}
      onClose={() => {}}
      onCreated={() => {}}
      onOpenSettings={() => {}}
    />,
  )
}

afterEach(() => vi.restoreAllMocks())

describe('a failed creation', () => {
  it('is replaced by the next attempt rather than joined by it', async () => {
    const created = vi
      .spyOn(api, 'createProject')
      .mockResolvedValueOnce({
        id: 'proj-1',
        status: 'error',
        status_detail: 'git clone failed',
      } as never)
      .mockResolvedValueOnce({
        id: 'proj-2',
        status: 'error',
        status_detail: 'git clone failed again',
      } as never)
    const deleted = vi
      .spyOn(api, 'deleteProject')
      .mockResolvedValue(undefined as never)

    renderForm()
    fireEvent.change(screen.getByLabelText(/project name/i), {
      target: { value: 'portobello' },
    })
    fireEvent.click(screen.getByRole('button', { name: /create project/i }))

    // The failure is shown rather than thrown away, and the button now offers
    // the thing it is actually going to do.
    await waitFor(() =>
      expect(screen.getByText(/git clone failed/i)).toBeInTheDocument(),
    )
    const again = await screen.findByRole('button', { name: /try again/i })

    fireEvent.click(again)

    await waitFor(() => expect(created).toHaveBeenCalledTimes(2))
    // The first attempt is cleared away before the second is made.
    expect(deleted).toHaveBeenCalledWith('proj-1', true)
  })

  it('does not try to delete anything on a first attempt', async () => {
    vi.spyOn(api, 'createProject').mockResolvedValue({
      id: 'proj-1',
      status: 'error',
      status_detail: 'nope',
    } as never)
    const deleted = vi
      .spyOn(api, 'deleteProject')
      .mockResolvedValue(undefined as never)

    renderForm()
    fireEvent.change(screen.getByLabelText(/project name/i), {
      target: { value: 'portobello' },
    })
    fireEvent.click(screen.getByRole('button', { name: /create project/i }))

    await waitFor(() => expect(screen.getByText(/nope/i)).toBeInTheDocument())
    expect(deleted).not.toHaveBeenCalled()
  })
})
