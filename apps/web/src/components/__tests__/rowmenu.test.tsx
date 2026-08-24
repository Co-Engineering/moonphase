import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RowMenu } from '../RowMenu'

/**
 * Removing a server, project or session was possible all along, at the bottom
 * of a detail panel you had to know to open — so it was reported as impossible.
 */
describe('row actions', () => {
  it('asks before doing something destructive', () => {
    const onSelect = vi.fn()
    render(
      <RowMenu
        label="worker"
        actions={[{ label: 'Remove server', danger: true, onSelect }]}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Actions for worker' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove server' }))

    // Not yet: the first click asks.
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove server' }))
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('lets the question be declined', () => {
    const onSelect = vi.fn()
    render(
      <RowMenu label="worker" actions={[{ label: 'Remove', danger: true, onSelect }]} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Actions for worker' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does what a harmless action says immediately', () => {
    const onSelect = vi.fn()
    render(<RowMenu label="worker" actions={[{ label: 'Share', onSelect }]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Actions for worker' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Share' }))

    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('says why an action is unavailable rather than hiding it', () => {
    // A menu that silently omits things teaches people it is incomplete. Saying
    // "not yours" answers the question they were about to ask.
    render(
      <RowMenu
        label="worker"
        actions={[
          { label: 'Remove', danger: true, disabledReason: 'not yours', onSelect: vi.fn() },
        ]}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Actions for worker' }))
    expect(screen.getByText('not yours')).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Remove' })).toBeNull()
  })
})
