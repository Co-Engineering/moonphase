import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ClaudeConfigDialog } from '../ClaudeConfigDialog'
import type { ClaudeConfig } from '../../lib/api'

afterEach(() => vi.restoreAllMocks())

const CONFIG_WITH_AN_MCP_SERVER: ClaudeConfig = {
  claude_settings_json: null,
  claude_md: null,
  mcp_json: JSON.stringify({ mcpServers: { sentry: { url: 'https://example.com/mcp' } } }),
  skills: {},
  env_vars: {},
}

async function renderAndClickConnect(save: (input: ClaudeConfig) => Promise<unknown>) {
  render(
    <ClaudeConfigDialog
      title="Configure"
      note="applies here"
      load={() => Promise.resolve(CONFIG_WITH_AN_MCP_SERVER)}
      save={save}
      onClose={() => {}}
      mcpConnect={{ scope: 'project', projectId: 'proj-1' }}
    />,
  )
  fireEvent.click(await screen.findByRole('tab', { name: /mcp servers/i }))
  fireEvent.click(await screen.findByRole('button', { name: /connect/i }))
}

/**
 * The relay looks a server up in the *saved* config, not whatever is still
 * sitting in this dialog's own state — so pressing "Connect" on a server
 * that was only just typed in, without a Save in between, used to relay
 * against a server the container did not know about yet. Fixed by saving
 * first, only opening the connect flow once that succeeds.
 */
describe('connecting an MCP server from the project/session config dialog', () => {
  it('saves the current config before opening the connect dialog', async () => {
    const save = vi.fn().mockResolvedValue(undefined)
    await renderAndClickConnect(save)

    await waitFor(() => expect(save).toHaveBeenCalledWith(CONFIG_WITH_AN_MCP_SERVER))
  })

  it('does not open the connect dialog when the save fails', async () => {
    const save = vi.fn().mockRejectedValue(new Error('disk full'))
    await renderAndClickConnect(save)

    await screen.findByText(/disk full/)
    // The relay dialog names the server it is opening for — its absence is
    // the signal that it never opened.
    expect(screen.queryByText(/open this url/i)).not.toBeInTheDocument()
  })
})
