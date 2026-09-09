import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { McpEditor } from '../ClaudeConfig'

/**
 * "+ Add server" pushed a blank-named entry, which onChange round-tripped
 * straight back out again — serversInto drops any server whose name is
 * empty, so the row vanished before it ever rendered and the button looked
 * like it did nothing. A unique placeholder name survives that round-trip.
 */
describe('adding an MCP server', () => {
  it('actually adds a visible row, not a silently-dropped one', () => {
    let value = JSON.stringify({ mcpServers: {} })
    const onChange = (next: string | null) => {
      value = next ?? ''
    }

    const { rerender } = render(<McpEditor value={value} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }))
    rerender(<McpEditor value={value} onChange={onChange} />)

    expect(screen.getByDisplayValue('server')).toBeInTheDocument()
  })

  it('gives each successive blank server a distinct name', () => {
    let value = JSON.stringify({ mcpServers: {} })
    const onChange = (next: string | null) => {
      value = next ?? ''
    }

    const { rerender } = render(<McpEditor value={value} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }))
    rerender(<McpEditor value={value} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }))
    rerender(<McpEditor value={value} onChange={onChange} />)

    expect(screen.getByDisplayValue('server')).toBeInTheDocument()
    expect(screen.getByDisplayValue('server-1')).toBeInTheDocument()
  })
})
