import { describe, expect, it } from 'vitest'
import {
  formatRule,
  parseDoc,
  parseRule,
  rulesFrom,
  rulesInto,
  serializeDoc,
  serversFrom,
  serversInto,
  splitArgs,
} from './ClaudeConfig'

/**
 * The property that makes a structured editor safe to point at a file someone
 * already depends on: it must never lose what it does not understand.
 */
describe('unknown keys survive an edit', () => {
  it('keeps settings the form cannot show', () => {
    const original = {
      hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [] }] },
      statusLine: { type: 'command', command: 'echo hi' },
      permissions: { allow: ['Bash(ls)'] },
    }
    const edited = rulesInto(original, [
      { decision: 'deny', tool: 'Write', pattern: './secrets/**' },
    ])

    expect(edited.hooks).toEqual(original.hooks)
    expect(edited.statusLine).toEqual(original.statusLine)
    expect(edited.permissions).toEqual({ deny: ['Write(./secrets/**)'] })
  })

  it('keeps per-server MCP keys the form cannot show', () => {
    const original = {
      mcpServers: {
        db: {
          command: 'npx',
          args: ['-y', 'server'],
          // Not modelled by the form; must still be there afterwards.
          disabled: false,
          timeout: 30000,
        },
      },
    }
    const servers = serversFrom(original)
    const edited = serversInto(original, [{ ...servers[0], command: 'uvx' }])
    const db = (edited.mcpServers as Record<string, Record<string, unknown>>).db

    expect(db.command).toBe('uvx')
    expect(db.disabled).toBe(false)
    expect(db.timeout).toBe(30000)
  })
})

describe('permission rules', () => {
  it('round-trips a rule with a pattern', () => {
    const parsed = parseRule('Bash(npm run test:*)')
    expect(parsed).toEqual({ tool: 'Bash', pattern: 'npm run test:*' })
    expect(formatRule({ decision: 'allow', ...parsed })).toBe('Bash(npm run test:*)')
  })

  it('round-trips a bare tool', () => {
    expect(parseRule('WebFetch')).toEqual({ tool: 'WebFetch', pattern: '' })
    expect(formatRule({ decision: 'deny', tool: 'WebFetch', pattern: '' })).toBe('WebFetch')
  })

  it('keeps parentheses inside a pattern', () => {
    // A glob or a command can legitimately contain them, and splitting on the
    // first one would silently truncate the rule.
    expect(parseRule('Bash(echo (hi))')).toEqual({ tool: 'Bash', pattern: 'echo (hi)' })
  })

  it('reads every decision list', () => {
    const rules = rulesFrom({
      permissions: { allow: ['Read'], ask: ['Edit(*.ts)'], deny: ['Bash(rm *)'] },
    })
    expect(rules).toEqual([
      { decision: 'allow', tool: 'Read', pattern: '' },
      { decision: 'ask', tool: 'Edit', pattern: '*.ts' },
      { decision: 'deny', tool: 'Bash', pattern: 'rm *' },
    ])
  })

  it('drops a decision list once it is empty', () => {
    const edited = rulesInto({ permissions: { allow: ['Read'], defaultMode: 'plan' } }, [])
    // The rules are gone; the unrelated key beside them is not.
    expect(edited.permissions).toEqual({ defaultMode: 'plan' })
  })

  it('ignores a rule with no tool rather than writing an empty string', () => {
    const edited = rulesInto({}, [{ decision: 'allow', tool: '  ', pattern: 'x' }])
    expect(edited.permissions).toBeUndefined()
  })
})

describe('mcp servers', () => {
  it('infers the transport when it is not declared', () => {
    const servers = serversFrom({
      mcpServers: {
        local: { command: 'npx', args: ['-y', 'thing'] },
        remote: { url: 'https://example.com/mcp' },
        sse: { type: 'sse', url: 'https://example.com/sse' },
      },
    })
    expect(servers.map((s) => [s.name, s.transport])).toEqual([
      ['local', 'stdio'],
      ['remote', 'http'],
      ['sse', 'sse'],
    ])
  })

  it('joins and splits arguments without breaking on quoted spaces', () => {
    expect(splitArgs('-y pkg "/path with spaces" --flag')).toEqual([
      '-y',
      'pkg',
      '/path with spaces',
      '--flag',
    ])
  })

  it('drops the keys that belong to the other transport when it changes', () => {
    const before = { mcpServers: { s: { command: 'npx', args: ['x'] } } }
    const [server] = serversFrom(before)
    const after = serversInto(before, [
      { ...server, transport: 'http', url: 'https://example.com/mcp' },
    ])
    const entry = (after.mcpServers as Record<string, Record<string, unknown>>).s
    // A leftover `command` beside a `url` is the config that half-works and
    // cannot be debugged from the file.
    expect(entry.command).toBeUndefined()
    expect(entry.args).toBeUndefined()
    expect(entry).toMatchObject({ type: 'http', url: 'https://example.com/mcp' })
  })

  it('removes the container once the last server goes', () => {
    expect(serversInto({ mcpServers: { s: { command: 'x' } } }, []).mcpServers).toBeUndefined()
  })
})

describe('documents', () => {
  it('treats a non-object as empty rather than throwing', () => {
    expect(parseDoc('[1,2]')).toEqual({})
    expect(parseDoc('not json')).toEqual({})
    expect(parseDoc(null)).toEqual({})
  })

  it('serialises an empty document to null so nothing is written', () => {
    expect(serializeDoc({})).toBeNull()
    expect(serializeDoc({ permissions: {}, model: '' })).toBeNull()
  })

  it('keeps a meaningful false', () => {
    // `includeCoAuthoredBy: false` is the whole point of setting it, and an
    // over-eager empty check would delete exactly the value someone chose.
    expect(serializeDoc({ includeCoAuthoredBy: false })).toBe(
      '{\n  "includeCoAuthoredBy": false\n}',
    )
  })
})
