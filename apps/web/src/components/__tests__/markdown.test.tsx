import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Markdown } from '../Markdown'

/**
 * This is model output landing verbatim in the feed, so what matters is that
 * real formatting renders as formatting, an actual `<script>`-shaped string
 * in a reply never becomes a live element, and a link never silently
 * navigates the tab it's read in away from the session.
 */
describe('Markdown', () => {
  it('renders emphasis and inline code as their elements, not literal punctuation', () => {
    render(<Markdown text={'**bold**, *italic*, and `inline code`'} />)
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('italic').tagName).toBe('EM')
    const code = screen.getByText('inline code')
    expect(code.tagName).toBe('CODE')
    expect(code.className).toBe('md-code-inline')
  })

  it('renders a fenced code block distinctly from inline code', () => {
    const { container } = render(<Markdown text={'```js\nconst x = 1\n```'} />)
    const block = container.querySelector('pre code') as HTMLElement
    expect(block).toBeTruthy()
    expect(block.className).toContain('md-code-block')
    expect(block.className).toContain('language-js')
    expect(block.textContent).toBe('const x = 1\n')
  })

  it('renders a GFM table', () => {
    render(<Markdown text={'| a | b |\n| - | - |\n| 1 | 2 |'} />)
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('opens links in a new tab without granting the new page a handle back', () => {
    render(<Markdown text={'[docs](https://example.com/docs)'} />)
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('href', 'https://example.com/docs')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(link.getAttribute('rel')).toContain('noreferrer')
  })

  it('never renders raw HTML embedded in the text as live elements', () => {
    const { container } = render(
      <Markdown text={'before <script>window.pwned = true</script> after'} />,
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.textContent).toContain('<script>')
  })

  it('renders a task list checkbox as a disabled control, not a form input', () => {
    render(<Markdown text={'- [x] done\n- [ ] not done'} />)
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes).toHaveLength(2)
    expect(boxes[0].checked).toBe(true)
    expect(boxes[0].disabled).toBe(true)
  })
})
