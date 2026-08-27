import { useEffect, useState } from 'react'
import { ClaudeConfigFields, type ClaudeConfigValue } from './ClaudeConfig'
import { McpConnectDialog, type McpConnectTarget } from './McpConnectDialog'
import type { ClaudeConfig } from '../lib/api'

const EMPTY: ClaudeConfigValue = {
  claude_settings_json: null,
  claude_md: null,
  mcp_json: null,
  skills: {},
  env_vars: {},
}

/**
 * Project- or session-level Claude Code config, in a modal of its own.
 *
 * The same four editors the global Settings screen uses, at a narrower scope
 * — `load`/`save` are the only thing that differs between "this project" and
 * "this session", so the dialog does not need to know which one it is.
 */
export function ClaudeConfigDialog({
  title,
  note,
  load,
  save,
  onClose,
  onSaved,
  mcpConnect,
}: {
  title: string
  note: string
  load: () => Promise<ClaudeConfig>
  save: (input: ClaudeConfig) => Promise<unknown>
  onClose: () => void
  onSaved?: () => void
  /**
   * Lets the MCP tab offer "Connect" for a server needing OAuth. Session
   * scope relays through that specific session; project scope has no one
   * session in hand, so the backend picks any one of the caller's own
   * running sessions in this project to carry it.
   */
  mcpConnect?: Extract<McpConnectTarget, { scope: 'session' | 'project' }>
}) {
  const [value, setValue] = useState<ClaudeConfigValue>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [connecting, setConnecting] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    load()
      .then((config) => {
        if (!cancelled) setValue(config)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // `load` is a fresh closure each render by design (it closes over the
    // project/session id); re-running it on every render would refetch on
    // every keystroke, so it is deliberately not in the dependency list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onSave = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await save(value)
      setNotice('Saved. Restart a harness in this project to pick it up.')
      onSaved?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>
        <p className="hint">{note}</p>

        {error && <div className="banner error">{error}</div>}
        {notice && <div className="banner">{notice}</div>}

        {loading ? (
          <p className="muted">Loading…</p>
        ) : (
          <ClaudeConfigFields
            value={value}
            onChange={setValue}
            claudeMdHint="Added to CLAUDE.md for this scope, alongside anything set above it"
            onConnectMcp={mcpConnect ? (name) => setConnecting(name) : undefined}
          />
        )}

        <div className="actions">
          <button className="primary" disabled={busy || loading} onClick={() => void onSave()}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <div className="spacer" />
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>

      {connecting && mcpConnect && (
        <McpConnectDialog
          target={mcpConnect}
          serverName={connecting}
          onClose={() => setConnecting(null)}
          onConnected={() => setConnecting(null)}
        />
      )}
    </div>
  )
}
