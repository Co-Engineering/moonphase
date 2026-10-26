import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectView } from '../App'
import { AccountDialog } from '../components/AccountPicker'
import { ClaudeConfigDialog } from '../components/ClaudeConfigDialog'
import { api, type ClaudeConfig, type HarnessAccount, type Project, type Session } from '../lib/api'
import { useHarnessAccounts } from '../lib/useHarnessAccounts'

vi.mock('../lib/supabase', () => ({
  accessToken: async () => 'test-token',
  client: () => ({ auth: {} }),
}))

// jsdom has no media queries, and the view defaults by screen width.
vi.stubGlobal('matchMedia', (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  onchange: null,
  dispatchEvent: () => false,
}))

afterEach(() => vi.restoreAllMocks())

const account = (over: Partial<HarnessAccount>): HarnessAccount => ({
  id: 'a1',
  label: null,
  auth_mode: 'oauth',
  is_default: true,
  created_at: '',
  updated_at: '',
  ...over,
})

const claudeAccounts = [
  account({ id: 'c1', label: 'Personal' }),
  account({ id: 'c2', label: 'Client X', is_default: false }),
]
const googleAccounts = [
  account({ id: 'g1' }),
  account({ id: 'g2', label: 'Work', is_default: false }),
]

const project = (over: Partial<Project>): Project =>
  ({
    id: 'p1',
    name: 'gravity',
    status: 'running',
    access: 'admin',
    server_name: 'test',
    environment: 'debian',
    share_count: 0,
    status_detail: null,
    harness: 'claude_code',
    harness_credential_id: null,
    ...over,
  }) as unknown as Project

const claudeProject = project({ id: 'p-claude', harness: 'claude_code' })
const gravityProject = project({ id: 'p-gravity', harness: 'antigravity' })

/**
 * Accounts are per harness, and the backend refuses another harness's.
 *
 * The app loaded one list — Claude's — and gave it to every project, so an
 * Antigravity project's pickers listed Claude accounts, and choosing one came
 * back "No such account.". Each project has to get its own harness's.
 */
describe('loading accounts for the harnesses projects use', () => {
  it('gives each project its own harness’s accounts', async () => {
    const listed = vi
      .spyOn(api, 'harnessAccounts')
      .mockImplementation(async (harness = 'claude_code') =>
        harness === 'antigravity' ? googleAccounts : claudeAccounts,
      )

    const { result } = renderHook(() => useHarnessAccounts([claudeProject, gravityProject]))

    await waitFor(() => expect(result.current.accountsFor(gravityProject)).toEqual(googleAccounts))
    expect(result.current.accountsFor(claudeProject)).toEqual(claudeAccounts)
    expect(listed).toHaveBeenCalledWith('claude_code')
    expect(listed).toHaveBeenCalledWith('antigravity')
  })

  it('keeps Claude’s accounts when another harness’s fail to load', async () => {
    vi.spyOn(api, 'harnessAccounts').mockImplementation(async (harness = 'claude_code') => {
      if (harness === 'antigravity') throw new Error('Not Found')
      return claudeAccounts
    })

    const { result } = renderHook(() => useHarnessAccounts([claudeProject, gravityProject]))

    await waitFor(() => expect(result.current.accountsFor(claudeProject)).toEqual(claudeAccounts))
    expect(result.current.accountsFor(gravityProject)).toEqual([])
  })

  it('loads a signed-in harness’s accounts before anything here uses it', async () => {
    // A new session can be started on it, and its account picker needs them.
    const listed = vi
      .spyOn(api, 'harnessAccounts')
      .mockImplementation(async (harness = 'claude_code') =>
        harness === 'antigravity' ? googleAccounts : claudeAccounts,
      )

    const { result } = renderHook(() =>
      useHarnessAccounts([claudeProject], [], [
        { kind: 'claude_code', configured: true },
        { kind: 'antigravity', configured: true },
        { kind: 'opencode', configured: false },
      ]),
    )

    await waitFor(() => expect(result.current.accountsFor('antigravity')).toEqual(googleAccounts))
    expect(listed).not.toHaveBeenCalledWith('opencode')
  })

  it('asks for Claude’s alone while no project uses anything else', async () => {
    const listed = vi.spyOn(api, 'harnessAccounts').mockResolvedValue(claudeAccounts)

    const { result } = renderHook(() => useHarnessAccounts([claudeProject]))

    await waitFor(() => expect(result.current.accountsFor(claudeProject)).toEqual(claudeAccounts))
    expect(listed).toHaveBeenCalledTimes(1)
    expect(listed).toHaveBeenCalledWith('claude_code')
    // And another harness's project gets nothing rather than Claude's.
    expect(result.current.accountsFor(gravityProject)).toEqual([])
  })

  it('loads accounts for harnesses used by sessions in a project', async () => {
    const listed = vi
      .spyOn(api, 'harnessAccounts')
      .mockImplementation(async (harness = 'claude_code') =>
        harness === 'antigravity' ? googleAccounts : claudeAccounts,
      )

    const agySession = {
      tmux_session: 's-agy',
      harness: 'antigravity',
      project_id: 'p-claude',
    } as unknown as Session

    const { result } = renderHook(() =>
      useHarnessAccounts([claudeProject], [agySession]),
    )

    await waitFor(() => expect(result.current.accountsFor('antigravity')).toEqual(googleAccounts))
    expect(result.current.accountsFor(claudeProject)).toEqual(claudeAccounts)
    expect(listed).toHaveBeenCalledWith('claude_code')
    expect(listed).toHaveBeenCalledWith('antigravity')
  })
})

describe('an Antigravity project’s account pickers', () => {
  const view = () =>
    render(
      <ProjectView
        project={gravityProject}
        session={null}
        sessions={[]}
        accounts={googleAccounts}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )

  it('names the harness and lists its accounts, calling an unnamed one Google', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    view()
    await screen.findByText('main (default)')

    const picker = screen
      .getAllByLabelText<HTMLSelectElement>('Antigravity account')
      .find((el) => el.closest('.new-session'))!
    expect([...picker.options].map((o) => o.textContent)).toEqual([
      "Google account (project's)",
      'Google account',
      'Work',
    ])
    expect(screen.queryByLabelText('Claude account')).toBeNull()
    expect(screen.queryByText(/Claude subscription/)).toBeNull()
  })

  it('starts a session on the Antigravity account picked', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    const create = vi
      .spyOn(api, 'createSession')
      .mockResolvedValue({ tmux_session: 'g' } as unknown as Session)
    view()
    await screen.findByText('main (default)')

    const picker = screen
      .getAllByLabelText<HTMLSelectElement>('Antigravity account')
      .find((el) => el.closest('.new-session'))!
    fireEvent.change(picker, { target: { value: 'g2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start my session' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith('p-gravity', undefined, undefined, 'g2'),
    )
  })

  it('titles the sidebar’s account dialog by the harness', () => {
    render(
      <AccountDialog
        project={gravityProject}
        accounts={[googleAccounts[0]]}
        onChanged={() => {}}
        onClose={() => {}}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Antigravity account — gravity' })).toBeTruthy()
    expect(screen.getByText(/You have one Antigravity account/)).toBeTruthy()
  })

  it('leaves a Claude project’s dialog exactly as it was', () => {
    render(
      <AccountDialog
        project={claudeProject}
        accounts={[claudeAccounts[0]]}
        onChanged={() => {}}
        onClose={() => {}}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Claude account — gravity' })).toBeTruthy()
    expect(screen.getByText(/You have one Claude account/)).toBeTruthy()
  })
})

/**
 * Configure is Claude Code's editors. For another harness the settings.json
 * one does nothing at all — agy never reads `~/.claude/settings.json` — so
 * it is not offered, and the dialog says what does apply.
 */
describe('configuring a project on another harness', () => {
  const config: ClaudeConfig = {
    claude_settings_json: null,
    claude_md: null,
    mcp_json: null,
    skills: {},
    env_vars: {},
  }

  const dialog = (harness?: 'claude_code' | 'antigravity') =>
    render(
      <ClaudeConfigDialog
        harness={harness}
        title="Configure gravity"
        note="Applies to every session in this project."
        load={() => Promise.resolve(config)}
        save={() => Promise.resolve()}
        onClose={() => {}}
      />,
    )

  it('leaves out Claude Code’s permissions and says what applies', async () => {
    vi.spyOn(api, 'mcpOAuthConnections').mockResolvedValue([])
    dialog('antigravity')

    expect(await screen.findByRole('tab', { name: 'MCP servers' })).toBeTruthy()
    expect(screen.queryByRole('tab', { name: 'Permissions & behaviour' })).toBeNull()
    expect(screen.getByRole('tab', { name: 'Instructions' })).toBeTruthy()
    expect(screen.getByText(/~\/\.gemini\/AGENTS\.md/)).toBeTruthy()
  })

  it('changes nothing for Claude Code', async () => {
    vi.spyOn(api, 'mcpOAuthConnections').mockResolvedValue([])
    dialog()

    expect(await screen.findByRole('tab', { name: 'Permissions & behaviour' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'CLAUDE.md' })).toBeTruthy()
    expect(screen.queryByText(/permissions file does not apply/)).toBeNull()
  })
})

describe('starting a session on another harness in the same project', () => {
  const testHarnesses = [
    {
      kind: 'claude_code' as const,
      display_name: 'Claude Code',
      supported_auth_modes: ['oauth' as const],
      available: true,
      configured: true,
      login_supported: true,
    },
    {
      kind: 'antigravity' as const,
      display_name: 'Antigravity',
      supported_auth_modes: ['oauth' as const],
      available: true,
      configured: true,
      login_supported: true,
    },
  ]

  it('allows picking a different harness and passes it when creating the session', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    const create = vi
      .spyOn(api, 'createSession')
      .mockResolvedValue({ tmux_session: 'g' } as unknown as Session)

    const accountsFor = (h: Pick<Project, 'harness'> | string) => {
      const kind = typeof h === 'string' ? h : h.harness
      return kind === 'antigravity' ? googleAccounts : claudeAccounts
    }

    render(
      <ProjectView
        project={claudeProject}
        session={null}
        sessions={[]}
        accounts={claudeAccounts}
        accountsFor={accountsFor}
        harnesses={testHarnesses}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )
    await screen.findByText('main (default)')

    // Defaults to project harness (Claude Code)
    const harnessSelect = screen.getByLabelText<HTMLSelectElement>('Agent harness')
    expect(harnessSelect.value).toBe('claude_code')
    expect(screen.getByLabelText('Claude account')).toBeTruthy()

    // Switch to Antigravity
    fireEvent.change(harnessSelect, { target: { value: 'antigravity' } })

    // Account picker switches to Antigravity account
    const accountPicker = screen.getByLabelText<HTMLSelectElement>('Antigravity account')
    expect(accountPicker).toBeTruthy()
    expect([...accountPicker.options].map((o) => o.textContent)).toEqual([
      "Antigravity's default",
      'Google account',
      'Work',
    ])

    // Pick work account and start session
    fireEvent.change(accountPicker, { target: { value: 'g2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start my session' }))

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith('p-claude', undefined, undefined, 'g2', 'antigravity'),
    )
  })

  it('does not carry a harness picked in one project over to the next', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    const view = (p: Project) => (
      <ProjectView
        project={p}
        session={null}
        sessions={[]}
        accounts={claudeAccounts}
        harnesses={testHarnesses}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />
    )
    const { rerender } = render(view(claudeProject))
    await screen.findByText('main (default)')
    fireEvent.change(screen.getByLabelText('Agent harness'), { target: { value: 'antigravity' } })

    // The same view, now showing another Claude project.
    rerender(view(project({ id: 'p-other', harness: 'claude_code' })))

    expect(screen.getByLabelText<HTMLSelectElement>('Agent harness').value).toBe('claude_code')
  })

  it('offers every harness, so the choice is there before a second sign-in', async () => {
    // Only signed-in harnesses were listed, so with one account there was no
    // choice to see at all, and nothing saying there would be.
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])
    render(
      <ProjectView
        project={claudeProject}
        session={null}
        sessions={[]}
        accounts={claudeAccounts}
        harnesses={[
          testHarnesses[0],
          { ...testHarnesses[1], configured: false },
        ]}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )
    await screen.findByText('main (default)')

    const picker = screen.getByLabelText<HTMLSelectElement>('Agent harness')
    const agy = [...picker.options].find((o) => o.value === 'antigravity')!
    expect(agy.disabled).toBe(true)
    expect(agy.textContent).toMatch(/sign in under settings/i)
    expect(screen.getByText('Agent')).toBeInTheDocument()
  })

  it('displays harness badges when sessions use different harnesses', async () => {
    vi.spyOn(api, 'branches').mockResolvedValue(['main'])

    const sessions = [
      {
        id: 's-claude',
        tmux_session: 'claude-sess',
        name: 'claude-sess',
        harness: 'claude_code',
        is_mine: true,
        is_running: true,
        preview_ports: [],
        created_at: '',
      },
      {
        id: 's-agy',
        tmux_session: 'agy-sess',
        name: 'agy-sess',
        harness: 'antigravity',
        is_mine: true,
        is_running: true,
        preview_ports: [],
        created_at: '',
      },
    ] as unknown as Session[]

    render(
      <ProjectView
        project={claudeProject}
        session={null}
        sessions={sessions}
        accounts={claudeAccounts}
        harnesses={testHarnesses}
        onEnter={() => {}}
        onChanged={() => {}}
        onToggleSidebar={() => {}}
        onShare={() => {}}
        onRemoved={() => {}}
      />,
    )
    await screen.findByText('main (default)')

    expect(screen.getByText(/yours · Antigravity/)).toBeTruthy()
    expect(screen.getByText(/yours · Claude/)).toBeTruthy()
  })
})
