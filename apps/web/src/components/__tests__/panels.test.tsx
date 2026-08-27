import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Usage, UsageStrip, compact, money, resetLabel, untilLabel } from '../Usage'
import { ClaudeConfigFields, McpEditor, SettingsEditor, SkillsEditor } from '../ClaudeConfig'

// The API layer refuses to call out without a token, which is correct and
// nothing to do with what these tests are checking.
vi.mock('../../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: { signOut: async () => {} } }),
}))

/**
 * Do the new panels render, and do they say the right thing.
 *
 * Written for the same reason as the App smoke test: `tsc` and the build were
 * both happy about a blank window once already. These mount the real
 * components with only the network stubbed, so a crash on the path someone
 * actually walks fails here instead.
 *
 * The assertions beyond "it rendered" are the two claims that make the screen
 * worth having: the headline number follows how you pay, and an unpriced model
 * never shows a dollar figure.
 */

const win = (over: Record<string, unknown> = {}) => ({
  label: 'Current session',
  hours: 5,
  started_at: '2026-08-17T09:00:00Z',
  resets_at: '2026-08-17T14:00:00Z',
  tokens: 120_000,
  cost: null,
  limit_tokens: null,
  percent: null,
  ...over,
})

const base = {
  billing: 'oauth' as const,
  hours: 168,
  tokens: 4_711_268,
  cost: null,
  session_window: win(),
  week_window: win({ label: 'Current week', hours: 168, tokens: 4_711_268 }),
  models: [
    {
      model: 'claude-sonnet-5',
      tokens: 4_711_268,
      input_tokens: 100,
      output_tokens: 33_947,
      cache_read_tokens: 4_000_000,
      cache_write_tokens: 677_221,
      thinking_tokens: 0,
      cost: null,
      priced: false,
    },
  ],
  projects: [{ project_id: 'p1', project_name: 'fresh-demo', tokens: 4_711_268, cost: null }],
  series: [],
}

function serve(usage: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/api/usage/prices') ? [] : usage
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('formatting', () => {
  it('shortens large token counts', () => {
    expect(compact(4_711_268)).toBe('4.7M')
    expect(compact(12_400)).toBe('12K')
    expect(compact(940)).toBe('940')
  })

  it('never renders an unknown cost as a number', () => {
    expect(money(null)).toBe('—')
    expect(money(0)).toBe('$0.00')
    // Rounding a real charge to $0.00 reads as free, which it is not.
    expect(money(0.004)).toBe('<$0.01')
  })
})

describe('window labels', () => {
  const now = new Date('2026-08-17T12:00:00Z')

  it('gives a clock time for a reset today', () => {
    expect(resetLabel('2026-08-17T14:00:00Z', now)).toMatch(/^Resets /)
  })

  it('counts down in a unit you can act on', () => {
    expect(untilLabel('2026-08-17T14:30:00Z', now)).toBe('2h 30m')
    expect(untilLabel('2026-08-17T12:20:00Z', now)).toBe('20m')
    expect(untilLabel('2026-08-11T12:00:00Z', now)).toBe('now')
  })
})

describe('Usage', () => {
  it('shows raw tokens and offers to set a limit when none is known', async () => {
    serve(base)
    render(<Usage onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('Current session')).toBeTruthy())
    expect(screen.getByText('120K tokens')).toBeTruthy()
    // No allowance was given, so no bar is drawn claiming one.
    expect(screen.getAllByText(/Set your plan limit/).length).toBe(2)
  })

  it('shows a percentage once the allowance is known', async () => {
    serve({
      ...base,
      session_window: win({ limit_tokens: 400_000, percent: 30 }),
    })
    render(<Usage onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('30% used')).toBeTruthy())
    expect(screen.getByText('120K of 400K')).toBeTruthy()
  })

  it('shows spend on an API key', async () => {
    serve({ ...base, billing: 'api_key', cost: 2.6185 })
    render(<Usage onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('$2.62')).toBeTruthy())
  })

  it('offers to fix an unpriced model rather than showing a dash', async () => {
    serve(base)
    render(<Usage onClose={() => {}} />)

    await waitFor(() => expect(screen.getByText('set rate')).toBeTruthy())
  })

  it('hides the strip when no window has opened', async () => {
    serve({ ...base, session_window: win({ started_at: null, resets_at: null, tokens: 0 }) })
    const { container } = render(<UsageStrip onOpen={() => {}} />)

    await waitFor(() => expect(container.querySelector('.usage-strip')).toBeNull())
  })
})

describe('SettingsEditor', () => {
  it('renders rules from a document and writes changes back as JSON', () => {
    const onChange = vi.fn()
    render(
      <SettingsEditor
        value={'{"permissions":{"allow":["Bash(ls)"]},"hooks":{"PreToolUse":[]}}'}
        onChange={onChange}
      />,
    )

    expect((screen.getByDisplayValue('Bash') as HTMLInputElement).value).toBe('Bash')
    // The form cannot edit hooks, so it must say they are there.
    expect(screen.getByText(/kept as-is/)).toBeTruthy()

    fireEvent.click(screen.getByText('+ Add rule'))
    // A rule with no tool yet is not written out as an empty string.
    expect(onChange).not.toHaveBeenCalledWith(expect.stringContaining('""'))
  })

  it('warns before permissions are bypassed', () => {
    render(<SettingsEditor value={'{"permissions":{"defaultMode":"bypassPermissions"}}'} onChange={vi.fn()} />)

    expect(screen.getByText(/without asking/)).toBeTruthy()
  })
})

describe('McpEditor', () => {
  it('shows a server and the fields its transport needs', () => {
    render(
      <McpEditor
        value={'{"mcpServers":{"db":{"command":"npx","args":["-y","srv"]}}}'}
        onChange={vi.fn()}
      />,
    )

    expect((screen.getByDisplayValue('db') as HTMLInputElement).value).toBe('db')
    expect(screen.getByDisplayValue('npx')).toBeTruthy()
    expect(screen.getByDisplayValue('-y srv')).toBeTruthy()
  })

  it('adds a server from a template', () => {
    const onChange = vi.fn()
    render(<McpEditor value={null} onChange={onChange} />)

    fireEvent.click(screen.getByText('+ Filesystem'))

    const written = String(onChange.mock.calls[0][0])
    expect(JSON.parse(written).mcpServers.filesystem.command).toBe('npx')
  })

  it('offers Connect for a named HTTP server, but not a stdio one, and only when wired up', () => {
    const value = JSON.stringify({
      mcpServers: {
        remote: { type: 'http', url: 'https://example.com/mcp' },
        local: { command: 'npx', args: ['x'] },
      },
    })

    const { rerender } = render(<McpEditor value={value} onChange={vi.fn()} />)
    expect(screen.queryByText('Connect')).toBeNull()

    const onConnect = vi.fn()
    rerender(<McpEditor value={value} onChange={vi.fn()} onConnect={onConnect} />)
    const connectButtons = screen.getAllByText('Connect')
    expect(connectButtons).toHaveLength(1)

    fireEvent.click(connectButtons[0])
    expect(onConnect).toHaveBeenCalledWith('remote')
  })
})

describe('SkillsEditor', () => {
  it('shows a skill by name with its body', () => {
    const { container } = render(
      <SkillsEditor value={{ reviewer: '# Reviewer\nBe terse.' }} onChange={vi.fn()} />,
    )

    expect((screen.getByDisplayValue('reviewer') as HTMLInputElement).value).toBe(
      'reviewer',
    )
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    expect(textarea.value).toBe('# Reviewer\nBe terse.')
  })

  it('adds a new skill without clobbering an existing default name', () => {
    const onChange = vi.fn()
    render(<SkillsEditor value={{ 'new-skill': 'first' }} onChange={onChange} />)

    fireEvent.click(screen.getByText('+ Add skill'))

    expect(onChange).toHaveBeenCalledWith({
      'new-skill': 'first',
      'new-skill-1': '',
    })
  })

  it('removes a skill by name', () => {
    const onChange = vi.fn()
    render(
      <SkillsEditor value={{ a: '1', b: '2' }} onChange={onChange} />,
    )

    fireEvent.click(screen.getAllByLabelText('Remove skill')[0])

    expect(onChange).toHaveBeenCalledWith({ b: '2' })
  })
})

describe('ClaudeConfigFields', () => {
  it('switches between the four scopes and edits each independently', () => {
    const onChange = vi.fn()
    render(
      <ClaudeConfigFields
        value={{
          claude_settings_json: null,
          claude_md: 'Some instructions',
          mcp_json: null,
          skills: {},
        }}
        onChange={onChange}
        claudeMdHint="applies here"
      />,
    )

    // Defaults to the permissions tab.
    expect(screen.getByText('+ Add rule')).toBeTruthy()

    fireEvent.click(screen.getByText('CLAUDE.md'))
    expect(screen.getByText('applies here')).toBeTruthy()
    expect(screen.getByDisplayValue('Some instructions')).toBeTruthy()

    fireEvent.click(screen.getByText('Skills'))
    expect(screen.getByText('+ Add skill')).toBeTruthy()
  })
})
