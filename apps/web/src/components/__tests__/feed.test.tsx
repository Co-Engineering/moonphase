import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FeedRow } from '../Feed'
import type { FeedEvent } from '../../lib/api'

/**
 * A screenshot is a `result` event like any other, distinguished only by
 * carrying image data — this checks it renders as an image rather than
 * being swallowed by the "successful results are noise" rule that drops
 * every other quiet result.
 */

function event(over: Partial<FeedEvent> = {}): FeedEvent {
  return {
    id: 'e1',
    kind: 'result',
    text: '',
    at: null,
    tool: null,
    ok: true,
    sidechain: false,
    diff: null,
    added: 0,
    removed: 0,
    truncated: false,
    image_media_type: null,
    image_data: null,
    ...over,
  }
}

describe('a result event carrying a screenshot', () => {
  it('renders the image, thumbnail-sized, even though the call succeeded', () => {
    render(
      <FeedRow event={event({ image_media_type: 'image/png', image_data: 'aGVsbG8=' })} />,
    )
    const img = screen.getByRole('img') as HTMLImageElement
    expect(img.src).toBe('data:image/png;base64,aGVsbG8=')
    expect(img.closest('.feed-screenshot')?.classList.contains('open')).toBe(false)
  })

  it('expands to full size on click, and shrinks back on a second click', () => {
    render(
      <FeedRow event={event({ image_media_type: 'image/png', image_data: 'aGVsbG8=' })} />,
    )
    const button = screen.getByRole('img').closest('.feed-screenshot') as HTMLElement
    fireEvent.click(button)
    expect(button.classList.contains('open')).toBe(true)
    fireEvent.click(button)
    expect(button.classList.contains('open')).toBe(false)
  })
})

describe('a result event with no image', () => {
  it('renders nothing when the call succeeded and said nothing', () => {
    const { container } = render(<FeedRow event={event()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('still renders the error when the call failed', () => {
    render(<FeedRow event={event({ ok: false, text: 'No such file or directory' })} />)
    expect(screen.getByText('No such file or directory')).toBeInTheDocument()
  })
})
