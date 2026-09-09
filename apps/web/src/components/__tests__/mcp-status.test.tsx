import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { McpEditor } from '../ClaudeConfig'
import type { McpOAuthConnectionInfo } from '../../lib/api'

const connectionInfo = (server_name: string): McpOAuthConnectionInfo => ({
  id: `conn-${server_name}`,
  server_name,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
})

const configWith = (mcpServers: Record<string, unknown>) =>
  JSON.stringify({ mcpServers })

/**
 * The complaint this fixes: adding an MCP server never showed whether it was
 * actually connected — the only place that knew lived on a different screen
 * entirely. These assert the row now says so, for every case that screen's
 * connection list can answer (and the one it structurally cannot: stdio).
 */
describe('MCP server connection status', () => {
  it('shows connected for an http server with a matching OAuth connection', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[connectionInfo('sentry')]}
      />,
    )

    expect(screen.getByText('Connected')).toBeInTheDocument()
  })

  it('shows not connected for an http server with no matching connection', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[]}
      />,
    )

    expect(screen.getByText('Not connected')).toBeInTheDocument()
  })

  it('does not claim a connection belonging to a differently-named server', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[connectionInfo('some-other-server')]}
      />,
    )

    expect(screen.getByText('Not connected')).toBeInTheDocument()
  })

  it('shows a local-process label for stdio servers instead of a connection status', () => {
    render(
      <McpEditor
        value={configWith({ filesystem: { command: 'npx', args: ['-y', 'server'] } })}
        onChange={() => {}}
        connections={[connectionInfo('filesystem')]}
      />,
    )

    // "Local process" also appears as the transport <select>'s option label,
    // so the status span is targeted by its title instead of by text.
    expect(
      screen.getByTitle("Claude Code starts this itself — Moonphase has no way to confirm it's running"),
    ).toHaveTextContent('Local process')
    expect(screen.queryByText('Connected')).not.toBeInTheDocument()
  })

  it('offers Reconnect instead of Connect once a server is already connected', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[connectionInfo('sentry')]}
        onConnect={() => {}}
      />,
    )

    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
  })
})
