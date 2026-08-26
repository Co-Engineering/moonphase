import { useMemo, useState } from 'react'

/**
 * Claude Code configuration, without writing JSON.
 *
 * The old screen was three textareas and a promise that you knew the schema.
 * That is fine if you have it memorised and hostile if you do not: nothing
 * tells you that permission rules are `Tool(pattern)` strings, that an MCP
 * server is keyed by name inside `mcpServers`, or that a stdio server takes
 * `command` and `args` while an HTTP one takes `url`. You found out by pasting
 * something, restarting a harness, and watching it not work.
 *
 * These editors know the shape. Two properties make them safe to use on a file
 * you already care about:
 *
 * 1. **Unknown keys survive.** The parsed document is kept whole and the form
 *    writes back into it, so `hooks`, `statusLine`, or anything Anthropic ships
 *    next month is preserved even though nothing here can edit it. An editor
 *    that silently drops what it does not understand is worse than a textarea.
 * 2. **The JSON is still there.** Every editor has a raw tab showing the exact
 *    file. Structure is a convenience, not a cage — and it is the honest way to
 *    show what the form is actually doing.
 */

export type Doc = Record<string, unknown>

export function parseDoc(text: string | null): Doc {
  if (!text || !text.trim()) return {}
  try {
    const parsed: unknown = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Doc)
      : {}
  } catch {
    return {}
  }
}

/** Empty objects and empty strings are noise in a config file, so drop them. */
export function serializeDoc(doc: Doc): string | null {
  const clean: Doc = {}
  for (const [key, value] of Object.entries(doc)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value) && value.length === 0) continue
    if (
      typeof value === 'object' &&
      !Array.isArray(value) &&
      Object.keys(value as Doc).length === 0
    )
      continue
    clean[key] = value
  }
  return Object.keys(clean).length === 0 ? null : JSON.stringify(clean, null, 2)
}

// --- permission rules --------------------------------------------------------

/**
 * The tools a rule can name. Not exhaustive on purpose — the field is a
 * combobox, so anything Claude Code adds can still be typed in, and these are
 * only the ones worth not having to remember.
 */
const TOOLS = [
  'Bash',
  'Read',
  'Edit',
  'Write',
  'Glob',
  'Grep',
  'WebFetch',
  'WebSearch',
  'Task',
  'NotebookEdit',
]

const DECISIONS = ['allow', 'ask', 'deny'] as const
type Decision = (typeof DECISIONS)[number]

export interface Rule {
  decision: Decision
  tool: string
  /** The part in parentheses. Empty means the rule covers the whole tool. */
  pattern: string
}

export function parseRule(raw: string): { tool: string; pattern: string } {
  const match = /^([^(]+)\((.*)\)$/s.exec(raw.trim())
  if (match) return { tool: match[1].trim(), pattern: match[2] }
  return { tool: raw.trim(), pattern: '' }
}

export function formatRule(rule: Rule): string {
  return rule.pattern.trim() ? `${rule.tool}(${rule.pattern.trim()})` : rule.tool
}

export function rulesFrom(doc: Doc): Rule[] {
  const permissions = (doc.permissions ?? {}) as Doc
  const rules: Rule[] = []
  for (const decision of DECISIONS) {
    const list = permissions[decision]
    if (!Array.isArray(list)) continue
    for (const raw of list) {
      if (typeof raw !== 'string') continue
      rules.push({ decision, ...parseRule(raw) })
    }
  }
  return rules
}

/** Write rules back without disturbing the other permission keys. */
export function rulesInto(doc: Doc, rules: Rule[]): Doc {
  const permissions = { ...((doc.permissions ?? {}) as Doc) }
  for (const decision of DECISIONS) {
    const list = rules
      .filter((rule) => rule.decision === decision && rule.tool.trim())
      .map(formatRule)
    if (list.length > 0) permissions[decision] = list
    else delete permissions[decision]
  }
  const next = { ...doc }
  if (Object.keys(permissions).length > 0) next.permissions = permissions
  else delete next.permissions
  return next
}

// --- settings.json -----------------------------------------------------------

const MODES = [
  { value: '', label: 'Ask each time (default)' },
  { value: 'acceptEdits', label: 'Accept file edits automatically' },
  { value: 'plan', label: 'Plan mode — read only until you approve' },
  { value: 'bypassPermissions', label: 'Bypass all permission prompts' },
]

interface SettingsProps {
  value: string | null
  onChange: (next: string | null) => void
}

export function SettingsEditor({ value, onChange }: SettingsProps) {
  const [raw, setRaw] = useState(false)
  const doc = useMemo(() => parseDoc(value), [value])
  const rules = useMemo(() => rulesFrom(doc), [doc])
  const permissions = (doc.permissions ?? {}) as Doc

  const update = (next: Doc) => onChange(serializeDoc(next))
  const setPermission = (key: string, next: unknown) => {
    const merged = { ...permissions }
    if (next === '' || next === null || next === undefined) delete merged[key]
    else merged[key] = next
    const outer = { ...doc }
    if (Object.keys(merged).length > 0) outer.permissions = merged
    else delete outer.permissions
    update(outer)
  }

  if (raw) {
    return (
      <RawTab
        value={value}
        onChange={onChange}
        onBack={() => setRaw(false)}
        placeholder={'{\n  "permissions": {\n    "allow": ["Bash(npm run test:*)"]\n  }\n}'}
      />
    )
  }

  return (
    <div className="config-editor">
      <div className="config-head">
        <span>
          Written to <code>~/.claude/settings.json</code> in every session
        </span>
        <button className="ghost small" onClick={() => setRaw(true)}>
          Edit as JSON
        </button>
      </div>

      <label>
        <span>When Claude wants to do something</span>
        <select
          value={(permissions.defaultMode as string) ?? ''}
          onChange={(event) => setPermission('defaultMode', event.target.value)}
        >
          {MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </select>
      </label>
      {permissions.defaultMode === 'bypassPermissions' && (
        <p className="warn-note">
          Every tool runs without asking, including commands that delete files or
          reach the network. Reasonable in a throwaway container, and this is one —
          but it is your account and your repository inside it.
        </p>
      )}

      <h4>Permission rules</h4>
      <p className="muted small">
        Exceptions to the setting above. A rule with no pattern covers the whole
        tool; a pattern narrows it — <code>npm run test:*</code> under Bash allows
        those commands and nothing else.
      </p>
      <RuleList rules={rules} onChange={(next) => update(rulesInto(doc, next))} />

      <h4>Model</h4>
      <label>
        <span>Overrides the account default for every session</span>
        <input
          value={(doc.model as string) ?? ''}
          onChange={(event) => update({ ...doc, model: event.target.value })}
          placeholder="leave blank to use the account default"
          list="model-suggestions"
        />
        <datalist id="model-suggestions">
          <option value="claude-opus-5" />
          <option value="claude-sonnet-5" />
          <option value="claude-haiku-4-5" />
        </datalist>
      </label>

      <h4>Housekeeping</h4>
      <label className="check">
        <input
          type="checkbox"
          checked={doc.includeCoAuthoredBy !== false}
          onChange={(event) =>
            update({
              ...doc,
              // Only written when turned off: the default is on, and a config
              // file should say what you changed, not restate the defaults.
              ...(event.target.checked
                ? { includeCoAuthoredBy: undefined }
                : { includeCoAuthoredBy: false }),
            })
          }
        />
        <span>Add a Co-Authored-By line to commits Claude makes</span>
      </label>
      <label>
        <span>Keep chat transcripts for (days)</span>
        <input
          type="number"
          min="1"
          value={(doc.cleanupPeriodDays as number) ?? ''}
          onChange={(event) =>
            update({
              ...doc,
              cleanupPeriodDays: event.target.value ? Number(event.target.value) : undefined,
            })
          }
          placeholder="30"
        />
      </label>

      <UnknownKeys doc={doc} known={KNOWN_SETTINGS} />
    </div>
  )
}

const KNOWN_SETTINGS = [
  'permissions',
  'model',
  'includeCoAuthoredBy',
  'cleanupPeriodDays',
]

function RuleList({
  rules,
  onChange,
}: {
  rules: Rule[]
  onChange: (next: Rule[]) => void
}) {
  const set = (index: number, patch: Partial<Rule>) =>
    onChange(rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)))

  return (
    <div className="rule-list">
      {rules.map((rule, index) => (
        <div className="rule-row" key={index}>
          <select
            value={rule.decision}
            onChange={(event) => set(index, { decision: event.target.value as Decision })}
            className={`decision decision-${rule.decision}`}
          >
            <option value="allow">Allow</option>
            <option value="ask">Ask</option>
            <option value="deny">Deny</option>
          </select>
          <input
            value={rule.tool}
            onChange={(event) => set(index, { tool: event.target.value })}
            placeholder="Tool"
            list="tool-suggestions"
          />
          <input
            value={rule.pattern}
            onChange={(event) => set(index, { pattern: event.target.value })}
            placeholder="pattern (optional)"
          />
          <button
            className="ghost small"
            onClick={() => onChange(rules.filter((_, i) => i !== index))}
            aria-label="Remove rule"
          >
            ✕
          </button>
        </div>
      ))}
      <datalist id="tool-suggestions">
        {TOOLS.map((tool) => (
          <option key={tool} value={tool} />
        ))}
      </datalist>
      <button
        className="ghost small"
        onClick={() => onChange([...rules, { decision: 'allow', tool: '', pattern: '' }])}
      >
        + Add rule
      </button>
    </div>
  )
}

// --- .mcp.json ---------------------------------------------------------------

export interface McpServer {
  name: string
  transport: 'stdio' | 'http' | 'sse'
  command: string
  args: string
  url: string
  env: Record<string, string>
  headers: Record<string, string>
}

export function serversFrom(doc: Doc): McpServer[] {
  const servers = (doc.mcpServers ?? {}) as Doc
  return Object.entries(servers).map(([name, raw]) => {
    const entry = (raw ?? {}) as Doc
    const declared = typeof entry.type === 'string' ? entry.type : ''
    // The transport is usually implied rather than declared: a `url` means
    // remote, a `command` means a local process.
    const transport: McpServer['transport'] =
      declared === 'http' || declared === 'sse'
        ? declared
        : entry.url
          ? 'http'
          : 'stdio'
    return {
      name,
      transport,
      command: typeof entry.command === 'string' ? entry.command : '',
      args: Array.isArray(entry.args) ? entry.args.join(' ') : '',
      url: typeof entry.url === 'string' ? entry.url : '',
      env: (entry.env ?? {}) as Record<string, string>,
      headers: (entry.headers ?? {}) as Record<string, string>,
    }
  })
}

/**
 * Split a command line into arguments, respecting quotes.
 *
 * Naive splitting on spaces breaks the moment a path or a JSON argument
 * contains one, which is exactly the case people hit and cannot debug.
 */
export function splitArgs(line: string): string[] {
  const out: string[] = []
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(line)) !== null) {
    out.push(match[1] ?? match[2] ?? match[3])
  }
  return out
}

export function serversInto(doc: Doc, servers: McpServer[]): Doc {
  const existing = (doc.mcpServers ?? {}) as Doc
  const next: Doc = {}
  for (const server of servers) {
    const name = server.name.trim()
    if (!name) continue
    // Keep anything this form does not model on the server it belongs to.
    const previous = (existing[name] ?? {}) as Doc
    const entry: Doc = { ...previous }
    delete entry.command
    delete entry.args
    delete entry.url
    delete entry.type
    delete entry.headers

    if (server.transport === 'stdio') {
      entry.command = server.command.trim()
      const args = splitArgs(server.args)
      if (args.length > 0) entry.args = args
      delete entry.env
      if (Object.keys(server.env).length > 0) entry.env = server.env
    } else {
      entry.type = server.transport
      entry.url = server.url.trim()
      if (Object.keys(server.headers).length > 0) entry.headers = server.headers
    }
    next[name] = entry
  }
  const outer = { ...doc }
  if (Object.keys(next).length > 0) outer.mcpServers = next
  else delete outer.mcpServers
  return outer
}

/** Servers common enough that typing the command from memory is a waste. */
const TEMPLATES: { label: string; server: Omit<McpServer, 'name'> & { name: string } }[] = [
  {
    label: 'Filesystem',
    server: {
      name: 'filesystem',
      transport: 'stdio',
      command: 'npx',
      args: '-y @modelcontextprotocol/server-filesystem /home/dev/sessions',
      url: '',
      env: {},
      headers: {},
    },
  },
  {
    label: 'Postgres',
    server: {
      name: 'postgres',
      transport: 'stdio',
      command: 'npx',
      args: '-y @modelcontextprotocol/server-postgres',
      url: '',
      env: { DATABASE_URL: '' },
      headers: {},
    },
  },
  {
    label: 'Browser',
    server: {
      name: 'browser',
      transport: 'stdio',
      command: 'npx',
      args: '-y @playwright/mcp@latest --headless --isolated',
      url: '',
      // Matches where the "Browser tools" built-in environment installs
      // Chromium (see environments.py) — headless needs no DISPLAY, but it
      // does need to find the browser binary, and that path is not npx's
      // default.
      env: { PLAYWRIGHT_BROWSERS_PATH: '/opt/playwright-browsers' },
      headers: {},
    },
  },
  {
    label: 'Remote (HTTP)',
    server: {
      name: 'remote',
      transport: 'http',
      command: '',
      args: '',
      url: 'https://example.com/mcp',
      env: {},
      headers: {},
    },
  },
]

export function McpEditor({ value, onChange }: SettingsProps) {
  const [raw, setRaw] = useState(false)
  const doc = useMemo(() => parseDoc(value), [value])
  const servers = useMemo(() => serversFrom(doc), [doc])

  const update = (next: McpServer[]) => onChange(serializeDoc(serversInto(doc, next)))
  const set = (index: number, patch: Partial<McpServer>) =>
    update(servers.map((server, i) => (i === index ? { ...server, ...patch } : server)))

  if (raw) {
    return (
      <RawTab
        value={value}
        onChange={onChange}
        onBack={() => setRaw(false)}
        placeholder={'{\n  "mcpServers": {}\n}'}
      />
    )
  }

  return (
    <div className="config-editor">
      <div className="config-head">
        <span>
          Written to <code>~/.claude/.mcp.json</code> in every session
        </span>
        <button className="ghost small" onClick={() => setRaw(true)}>
          Edit as JSON
        </button>
      </div>

      {servers.length === 0 && (
        <p className="muted small">
          No MCP servers configured. They give Claude tools beyond the ones it ships
          with — a database it can query, a filesystem outside the repo, an internal
          API.
        </p>
      )}

      {servers.map((server, index) => (
        <div className="mcp-server" key={index}>
          <div className="mcp-server-head">
            <input
              className="mcp-name"
              value={server.name}
              onChange={(event) => set(index, { name: event.target.value })}
              placeholder="name"
            />
            <select
              value={server.transport}
              onChange={(event) =>
                set(index, { transport: event.target.value as McpServer['transport'] })
              }
            >
              <option value="stdio">Local process</option>
              <option value="http">HTTP</option>
              <option value="sse">SSE</option>
            </select>
            <button
              className="ghost small"
              onClick={() => update(servers.filter((_, i) => i !== index))}
              aria-label="Remove server"
            >
              ✕
            </button>
          </div>

          {server.transport === 'stdio' ? (
            <>
              <label>
                <span>Command</span>
                <input
                  value={server.command}
                  onChange={(event) => set(index, { command: event.target.value })}
                  placeholder="npx"
                />
              </label>
              <label>
                <span>Arguments</span>
                <input
                  value={server.args}
                  onChange={(event) => set(index, { args: event.target.value })}
                  placeholder="-y @modelcontextprotocol/server-filesystem /path"
                />
              </label>
              <PairEditor
                label="Environment"
                hint="Values are written into the container as-is. Anything secret belongs in Environment variables, not here."
                pairs={server.env}
                onChange={(env) => set(index, { env })}
              />
            </>
          ) : (
            <>
              <label>
                <span>URL</span>
                <input
                  value={server.url}
                  onChange={(event) => set(index, { url: event.target.value })}
                  placeholder="https://example.com/mcp"
                />
              </label>
              <PairEditor
                label="Headers"
                pairs={server.headers}
                onChange={(headers) => set(index, { headers })}
              />
            </>
          )}
        </div>
      ))}

      <div className="mcp-add">
        <button
          className="ghost small"
          onClick={() =>
            update([
              ...servers,
              {
                name: '',
                transport: 'stdio',
                command: '',
                args: '',
                url: '',
                env: {},
                headers: {},
              },
            ])
          }
        >
          + Add server
        </button>
        {TEMPLATES.map((template) => (
          <button
            key={template.label}
            className="ghost small"
            onClick={() => update([...servers, { ...template.server }])}
          >
            + {template.label}
          </button>
        ))}
      </div>

      <UnknownKeys doc={doc} known={['mcpServers']} />
    </div>
  )
}

// --- shared ------------------------------------------------------------------

function PairEditor({
  label,
  hint,
  pairs,
  onChange,
}: {
  label: string
  hint?: string
  pairs: Record<string, string>
  onChange: (next: Record<string, string>) => void
}) {
  const entries = Object.entries(pairs)
  return (
    <div className="pair-editor">
      <span className="pair-label">{label}</span>
      {hint && <p className="muted small">{hint}</p>}
      {entries.map(([key, value], index) => (
        <div className="pair-row" key={index}>
          <input
            value={key}
            onChange={(event) => {
              const next = Object.fromEntries(
                entries.map(([k, v], i) => (i === index ? [event.target.value, v] : [k, v])),
              )
              onChange(next)
            }}
            placeholder="KEY"
          />
          <input
            value={value}
            onChange={(event) => onChange({ ...pairs, [key]: event.target.value })}
            placeholder="value"
          />
          <button
            className="ghost small"
            onClick={() =>
              onChange(Object.fromEntries(entries.filter((_, i) => i !== index)))
            }
            aria-label={`Remove ${key}`}
          >
            ✕
          </button>
        </div>
      ))}
      <button className="ghost small" onClick={() => onChange({ ...pairs, '': '' })}>
        + Add
      </button>
    </div>
  )
}

/**
 * Say plainly what the form is not showing.
 *
 * The keys are preserved either way, but a person editing through a form is
 * entitled to know that the file contains more than what is on screen —
 * otherwise the form quietly misrepresents the file as complete.
 */
function UnknownKeys({ doc, known }: { doc: Doc; known: string[] }) {
  const extra = Object.keys(doc).filter((key) => !known.includes(key))
  if (extra.length === 0) return null
  return (
    <p className="muted small config-extra">
      Also in this file and kept as-is: {extra.map((key) => <code key={key}>{key}</code>)}.
      Use <em>Edit as JSON</em> to change them.
    </p>
  )
}

function RawTab({
  value,
  onChange,
  onBack,
  placeholder,
}: {
  value: string | null
  onChange: (next: string | null) => void
  onBack: () => void
  placeholder: string
}) {
  const [text, setText] = useState(value ?? '')
  const [error, setError] = useState<string | null>(null)

  const apply = (next: string) => {
    setText(next)
    if (!next.trim()) {
      setError(null)
      onChange(null)
      return
    }
    try {
      const parsed: unknown = JSON.parse(next)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setError('Must be a JSON object.')
        return
      }
      setError(null)
      onChange(next)
    } catch (err) {
      // Held, not discarded: an incomplete document is what typing looks like,
      // and throwing it away mid-keystroke would make the tab unusable.
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="config-editor">
      <div className="config-head">
        <span>Raw JSON</span>
        <button className="ghost small" onClick={onBack}>
          Back to form
        </button>
      </div>
      <textarea
        value={text}
        onChange={(event) => apply(event.target.value)}
        placeholder={placeholder}
        rows={14}
        spellCheck={false}
      />
      {error ? (
        <p className="error small">{error} — not saved until this parses.</p>
      ) : (
        <p className="muted small">Valid JSON.</p>
      )}
    </div>
  )
}
