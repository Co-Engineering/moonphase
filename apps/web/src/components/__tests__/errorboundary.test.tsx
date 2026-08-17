import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { ErrorBoundary } from '../ErrorBoundary'

/**
 * The component that has to work when something else has already failed.
 *
 * Three separate bugs in this app have presented as a blank window, because
 * React unmounts the whole tree when a render throws and nothing catches it.
 * The symptom carried no information at all, so each one had to be found by
 * guessing. What is tested here is that the next one says something.
 */

function Boom({ throws }: { throws: boolean }): JSX.Element {
  if (throws) throw new Error('the pane exploded')
  return <p>working</p>
}

beforeEach(() => {
  // React logs caught errors to console.error by design. Silenced so a passing
  // run does not look like a failing one.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ErrorBoundary', () => {
  it('renders its children when nothing is wrong', () => {
    render(
      <ErrorBoundary>
        <Boom throws={false} />
      </ErrorBoundary>,
    )

    expect(screen.getByText('working')).toBeTruthy()
  })

  it('shows the error instead of emptying the window', () => {
    const { container } = render(
      <ErrorBoundary>
        <Boom throws />
      </ErrorBoundary>,
    )

    // The whole point: something is on screen.
    expect(container.textContent).toBeTruthy()
    expect(screen.getByText(/stopped working/)).toBeTruthy()
    // And it says what, which a blank screen never did.
    expect(screen.getByText(/the pane exploded/)).toBeTruthy()
  })

  it('says which part broke when it is told', () => {
    render(
      <ErrorBoundary what="This session">
        <Boom throws />
      </ErrorBoundary>,
    )

    expect(screen.getByText('This session stopped working')).toBeTruthy()
  })

  it('reassures that the servers are unaffected', () => {
    // The first thing someone thinks when the window breaks is that their
    // hour-long agent run died with it. It did not.
    render(
      <ErrorBoundary>
        <Boom throws />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/sessions are still running/)).toBeTruthy()
  })

  it('recovers without a reload when the cause has gone', () => {
    function Flaky() {
      const [broken, setBroken] = useState(true)
      return (
        <>
          <button onClick={() => setBroken(false)}>fix it</button>
          <ErrorBoundary onReset={() => {}}>
            <Boom throws={broken} />
          </ErrorBoundary>
        </>
      )
    }
    render(<Flaky />)

    expect(screen.getByText(/stopped working/)).toBeTruthy()
    fireEvent.click(screen.getByText('fix it'))
    fireEvent.click(screen.getByText('Try again'))

    expect(screen.getByText('working')).toBeTruthy()
  })

  it('calls back on reset so the owner can clear its own state', () => {
    const onReset = vi.fn()
    render(
      <ErrorBoundary onReset={onReset}>
        <Boom throws />
      </ErrorBoundary>,
    )

    fireEvent.click(screen.getByText('Try again'))
    expect(onReset).toHaveBeenCalledOnce()
  })

  it('offers the details in a form someone can paste into a report', () => {
    const writeText = vi.fn()
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(
      <ErrorBoundary>
        <Boom throws />
      </ErrorBoundary>,
    )

    fireEvent.click(screen.getByText('Copy details'))
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('the pane exploded'))
    vi.unstubAllGlobals()
  })
})
