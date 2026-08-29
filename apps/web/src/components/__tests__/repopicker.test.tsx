import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RepoPicker } from '../RepoPicker'
import type { GitHubRepo } from '../../lib/api'

function repo(over: Partial<GitHubRepo> = {}): GitHubRepo {
  return {
    full_name: 'octocat/hello-world',
    clone_url: 'https://github.com/octocat/hello-world.git',
    private: false,
    description: null,
    pushed_at: null,
    ...over,
  }
}

/**
 * On a phone, this is the last field in a dialog that scrolls — focusing it
 * makes the browser scroll it above the keyboard, and the keyboard opening
 * resizes the viewport. Both used to close the dropdown outright, and
 * because the field's own displayed value depends on the dropdown being
 * open, every keystroke after that looked like it did nothing: the box was
 * there, but nothing typed into it ever appeared.
 */
describe('RepoPicker on a screen that scrolls or resizes while open', () => {
  it('stays open and keeps showing what was typed through a scroll event', () => {
    render(
      <RepoPicker
        value=""
        onChange={vi.fn()}
        repos={[repo(), repo({ full_name: 'octocat/other', clone_url: 'x' })]}
        loading={false}
        error={null}
      />,
    )
    const field = screen.getByPlaceholderText('Search your repositories…')
    fireEvent.focus(field)
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    // The mobile browser scrolling the dialog to bring this field above the
    // keyboard — not the user dismissing the picker.
    fireEvent.scroll(window)
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.change(field, { target: { value: 'octo' } })
    expect(field).toHaveValue('octo')
  })

  it('stays open and keeps showing what was typed through a resize event', () => {
    render(
      <RepoPicker
        value=""
        onChange={vi.fn()}
        repos={[repo()]}
        loading={false}
        error={null}
      />,
    )
    const field = screen.getByPlaceholderText('Search your repositories…')
    fireEvent.focus(field)

    // The on-screen keyboard opening — Android fires a window resize for
    // this; nothing here should read it as the user leaving the field.
    fireEvent(window, new Event('resize'))
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.change(field, { target: { value: 'hello' } })
    expect(field).toHaveValue('hello')
  })

  it('still closes on an actual click outside', () => {
    render(
      <RepoPicker value="" onChange={vi.fn()} repos={[repo()]} loading={false} error={null} />,
    )
    const field = screen.getByPlaceholderText('Search your repositories…')
    fireEvent.focus(field)
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})
