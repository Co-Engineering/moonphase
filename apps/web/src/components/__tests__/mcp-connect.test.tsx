import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { McpConnectDialog } from '../McpConnectDialog'
import { api, type McpOAuthConnection } from '../../lib/api'

afterEach(() => vi.restoreAllMocks())

const connection = (over: Partial<McpOAuthConnection>): McpOAuthConnection => ({
  session_id: 'relay-1',
  project_id: 'proj-from-response',
  state: 'awaiting_paste',
  url: 'https://example.com/oauth/authorize?x=1',
  detail: null,
  pane: null,
  ...over,
})

/**
 * "Connect" offered from a project's or the org's own Configure dialog has
 * no one session in hand — the backend auto-picks one and reports back which
 * project it landed in. This is the one piece of new client logic: calling
 * the right start endpoint per scope, and using the *response's* project id
 * for every poll/paste afterward rather than any project id the caller
 * happened to start with (there usually is not one, for the org scope).
 */
describe('McpConnectDialog', () => {
  it('starts a session-scoped connection with the session already in hand', async () => {
    const start = vi.spyOn(api, 'startMcpOAuth').mockResolvedValue(connection({}))
    vi.spyOn(api, 'pollMcpOAuth').mockResolvedValue(connection({}))

    render(
      <McpConnectDialog
        target={{ scope: 'session', projectId: 'proj-1', session: 'mine' }}
        serverName="sentry"
        onClose={() => {}}
        onConnected={() => {}}
      />,
    )

    await waitFor(() => expect(start).toHaveBeenCalledWith('proj-1', 'mine', 'sentry'))
  })

  it('starts a project-scoped connection with no session in hand', async () => {
    const start = vi
      .spyOn(api, 'startMcpOAuthForProject')
      .mockResolvedValue(connection({}))
    vi.spyOn(api, 'pollMcpOAuth').mockResolvedValue(connection({}))

    render(
      <McpConnectDialog
        target={{ scope: 'project', projectId: 'proj-1' }}
        serverName="sentry"
        onClose={() => {}}
        onConnected={() => {}}
      />,
    )

    await waitFor(() => expect(start).toHaveBeenCalledWith('proj-1', 'sentry'))
  })

  it('starts an org-scoped connection with neither project nor session in hand', async () => {
    const start = vi.spyOn(api, 'startMcpOAuthForOrg').mockResolvedValue(connection({}))
    vi.spyOn(api, 'pollMcpOAuth').mockResolvedValue(connection({}))

    render(
      <McpConnectDialog
        target={{ scope: 'org' }}
        serverName="sentry"
        onClose={() => {}}
        onConnected={() => {}}
      />,
    )

    await waitFor(() => expect(start).toHaveBeenCalledWith('sentry'))
  })

  it('polls and pastes using the project id the backend resolved, not the caller\'s', async () => {
    vi.spyOn(api, 'startMcpOAuthForOrg').mockResolvedValue(
      connection({ project_id: 'resolved-by-backend' }),
    )
    const poll = vi
      .spyOn(api, 'pollMcpOAuth')
      .mockResolvedValue(connection({ project_id: 'resolved-by-backend' }))

    render(
      <McpConnectDialog
        target={{ scope: 'org' }}
        serverName="sentry"
        onClose={() => {}}
        onConnected={() => {}}
      />,
    )

    await screen.findByText(/Open this URL and approve/)

    await waitFor(
      () => expect(poll).toHaveBeenCalledWith('resolved-by-backend', 'relay-1'),
      { timeout: 6000 },
    )
  })
})
