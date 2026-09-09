import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { McpEditor } from '../ClaudeConfig'
import type { McpHealth } from '../../lib/api'

const configWith = (mcpServers: Record<string, unknown>) =>
  JSON.stringify({ mcpServers })

/**
 * The button that actually asks a running session to check every server —
 * config existing, or even a saved OAuth credential, is not the same as a
 * server that answers.
 */
describe('checking MCP connections for real', () => {
  it('does not appear at all without a way to check', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Check connections' })).not.toBeInTheDocument()
  })

  it('shows the real result once the check resolves', async () => {
    const health: McpHealth[] = [
      { name: 'sentry', ok: false, detail: 'Failed to connect — timeout' },
    ]
    const onCheck = vi.fn().mockResolvedValue(health)

    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        onCheck={onCheck}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Check connections' }))

    await waitFor(() => expect(onCheck).toHaveBeenCalled())
    expect(await screen.findByTitle('Failed to connect — timeout')).toHaveTextContent('Failed')
  })

  it('a real failed check overrides a saved OAuth credential saying otherwise', async () => {
    const health: McpHealth[] = [{ name: 'sentry', ok: false, detail: 'HTTP 401: unauthorized' }]
    const onCheck = vi.fn().mockResolvedValue(health)

    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        onCheck={onCheck}
        connections={[
          { id: 'c1', server_name: 'sentry', created_at: '', updated_at: '' },
        ]}
      />,
    )

    // Before checking: the credential-presence heuristic.
    expect(screen.getByText('OAuth saved')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Check connections' }))
    await waitFor(() => expect(onCheck).toHaveBeenCalled())

    // After: the real, worse answer wins, and the stale one is gone.
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.queryByText('OAuth saved')).not.toBeInTheDocument()
  })

  it('reports a rejection as a banner rather than silently doing nothing', async () => {
    const onCheck = vi.fn().mockRejectedValue(new Error('needs a running session'))

    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        onCheck={onCheck}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Check connections' }))

    expect(await screen.findByText('needs a running session')).toBeInTheDocument()
  })
})
