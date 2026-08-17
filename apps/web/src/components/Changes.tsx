import { useMemo, useState } from 'react'
import * as api from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * What the agent actually did to the code.
 *
 * The feed says what it did and the terminal says what it is doing. Neither
 * answers the question you have after leaving it alone for an hour, and the
 * honest answer to that is a diff rather than a summary of one.
 *
 * Uncommitted work counts. An agent that has written twenty files and
 * committed none has still changed twenty files, and a review screen that
 * showed nothing until it committed would be worse than not having one.
 */

interface Props {
  projectId: string
  session: string
}

export function Changes({ projectId, session }: Props) {
  const changes = useResource(() => api.changes(projectId, session), [projectId, session], {
    pollMs: 15000,
  })
  const [open, setOpen] = useState<string | null>(null)

  const data = changes.data
  // Split the patch per file once, so opening one does not re-scan it.
  const patches = useMemo(() => splitPatch(data?.patch ?? ''), [data?.patch])

  if (changes.error) return <div className="error">{changes.error}</div>
  if (!data) return <p className="hint">Reading the worktree…</p>
  if (data.detail) return <div className="empty">{data.detail}</div>

  return (
    <div className="changes">
      <div className="changes-head">
        <span className="branch">{data.branch || 'no branch'}</span>
        <span className="muted">vs {data.base || 'base'}</span>
        <span className="stat-added">+{data.added}</span>
        <span className="stat-removed">−{data.removed}</span>
        <span className="muted">
          {data.files.length} file{data.files.length === 1 ? '' : 's'}
        </span>
      </div>

      {data.files.length === 0 ? (
        <div className="empty">
          <h3>Nothing has changed yet</h3>
          This session has not touched a file since it branched.
        </div>
      ) : (
        <div className="file-list">
          {data.files.map((file) => (
            <div key={file.path} className="file">
              <button
                className="file-head"
                onClick={() => setOpen(open === file.path ? null : file.path)}
              >
                <span className={`file-status status-${file.status}`}>
                  {file.status === 'untracked' ? 'new' : file.status === 'binary' ? 'bin' : '~'}
                </span>
                <span className="file-path">{file.path}</span>
                {file.added > 0 && <span className="stat-added">+{file.added}</span>}
                {file.removed > 0 && <span className="stat-removed">−{file.removed}</span>}
              </button>
              {open === file.path && (
                <Patch
                  text={patches.get(file.path) ?? ''}
                  status={file.status}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {data.truncated && (
        <p className="hint">
          The patch was cut short — this is a large change. Open the session to see all
          of it.
        </p>
      )}
    </div>
  )
}

function Patch({ text, status }: { text: string; status: string }) {
  if (status === 'untracked') {
    return (
      <p className="hint patch-note">
        A new file, so there is nothing to compare it against. Open the session to read
        it.
      </p>
    )
  }
  if (!text.trim()) {
    return <p className="hint patch-note">No textual diff for this file.</p>
  }
  return (
    <pre className="patch">
      {text.split('\n').map((line, index) => (
        <span key={index} className={`patch-line ${lineClass(line)}`}>
          {line || ' '}
        </span>
      ))}
    </pre>
  )
}

function lineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

/**
 * Cut a unified diff into one patch per file.
 *
 * Done here rather than on the server because the whole patch arrives in one
 * response anyway, and a second round trip per file opened would make browsing
 * a change feel like waiting.
 */
export function splitPatch(patch: string): Map<string, string> {
  const out = new Map<string, string>()
  if (!patch) return out

  let path: string | null = null
  let body: string[] = []

  const flush = () => {
    if (path) out.set(path, body.join('\n'))
  }

  for (const line of patch.split('\n')) {
    if (line.startsWith('diff --git ')) {
      flush()
      // `diff --git a/x b/x` — take the b-side, which is the path as it is now.
      const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line)
      path = match ? match[2] : null
      body = []
      continue
    }
    if (path) body.push(line)
  }
  flush()
  return out
}
