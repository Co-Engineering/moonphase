import { fireEvent, render, screen } from '@testing-library/react'
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
 * A configured server (one with a url/command already filled in) starts
 * collapsed; expanding it is how every test below reaches its Connect
 * button or its editable fields.
 */
function expand(serverName: string) {
  fireEvent.click(screen.getByText(serverName))
}

/**
 * The complaint this fixes: adding an MCP server never showed whether it was
 * actually connected — the only place that knew lived on a different screen
 * entirely. Without a live check (see mcp-health.test.tsx for that), the
 * best available signal is whether an OAuth credential is on file at all —
 * labelled as exactly that, not as "Connected", since a saved credential can
 * be stale or a server may never have needed one in the first place.
 */
describe('MCP server connection status', () => {
  it('shows a saved OAuth credential for an http server with a matching connection', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[connectionInfo('sentry')]}
      />,
    )

    expect(screen.getByText('OAuth saved')).toBeInTheDocument()
  })

  it('shows no saved credential for an http server with no matching connection', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[]}
      />,
    )

    expect(screen.getByText('No OAuth saved')).toBeInTheDocument()
  })

  it('does not claim a connection belonging to a differently-named server', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
        connections={[connectionInfo('some-other-server')]}
      />,
    )

    expect(screen.getByText('No OAuth saved')).toBeInTheDocument()
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
    // so the status is targeted by its title instead of by text.
    expect(
      screen.getByTitle('Claude Code starts this itself — check to see if it actually runs'),
    ).toHaveTextContent('Local process')
    expect(screen.queryByText('OAuth saved')).not.toBeInTheDocument()
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
    expand('sentry')

    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
  })
})

/**
 * Rows collapse by default so a long list of already-configured servers
 * reads as a list rather than a stack of full forms — the complaint that
 * design "gets cluttered" with many servers. A server still being filled in
 * (no url/command yet) is the one exception: there is nothing to collapse
 * to, so it starts open.
 */
describe('MCP server rows collapse when already configured', () => {
  it('a server with a url already set starts collapsed', () => {
    render(
      <McpEditor
        value={configWith({ sentry: { url: 'https://example.com/mcp' } })}
        onChange={() => {}}
      />,
    )

    // jsdom tracks <details>'s `open` as a plain DOM property rather than
    // modelling the layout/visibility a real browser applies from it, so
    // that property — not content visibility — is what a test can check.
    expect(screen.getByText('sentry').closest('details')).not.toHaveAttribute('open')
    expand('sentry')
    expect(screen.getByText('sentry').closest('details')).toHaveAttribute('open')
  })

  it('a freshly-added server with nothing filled in starts expanded', () => {
    let value = configWith({})
    const onChange = (next: string | null) => {
      value = next ?? ''
    }
    const { rerender } = render(<McpEditor value={value} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }))
    rerender(<McpEditor value={value} onChange={onChange} />)

    expect(screen.getByDisplayValue('server').closest('details')).toHaveAttribute('open')
  })
})
