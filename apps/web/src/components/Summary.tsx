import * as api from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * What happened, in one sentence.
 *
 * The feed is faithful and, for a lot of people, unreadable: forty tool calls,
 * each with a path and a diff. Someone who asked for a todo app and came back
 * an hour later wants to know that it made twelve files and installed three
 * packages, and to be able to look closer only if they want to.
 *
 * Counted, not generated. A summary written by a model could be wrong in ways
 * its reader has no way to check — and the whole reason this exists is that its
 * reader cannot check the diff. Counting is boring and it is always right.
 */

interface Props {
  projectId: string
  session: string
}

export function Summary({ projectId, session }: Props) {
  const summary = useResource(
    () => api.sessionSummary(projectId, session),
    [projectId, session],
    { pollMs: 30000 },
  )

  const data = summary.data
  if (!data || data.detail) return null

  const parts = describe(data)
  if (parts.length === 0 && !data.last_said) return null

  return (
    <div className="summary">
      <p className="summary-line">
        {parts.length > 0 ? sentence(parts) : 'Nothing has changed yet.'}
      </p>
      {data.last_said && <p className="summary-said">“{trim(data.last_said)}”</p>}
      {(data.created.length > 0 || data.edited.length > 0) && (
        <details className="summary-files">
          <summary>Which files</summary>
          <ul>
            {data.created.map((path) => (
              <li key={`c-${path}`}>
                <span className="file-tag new">new</span>
                {path}
              </li>
            ))}
            {data.edited.map((path) => (
              <li key={`e-${path}`}>
                <span className="file-tag">changed</span>
                {path}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

/**
 * The countable facts, as phrases.
 *
 * Only what happened. A zero is left out rather than reported as "installed 0
 * packages", which reads as a failure rather than as an absence.
 */
export function describe(d: api.Digest): string[] {
  const parts: string[] = []
  if (d.created.length > 0) {
    parts.push(`made ${count(d.created.length, 'new file')}`)
  }
  if (d.edited.length > 0) {
    parts.push(`changed ${count(d.edited.length, 'file')}`)
  }
  if (d.installs > 0) {
    parts.push(`installed ${count(d.installs, 'package')}`)
  }
  if (d.tests > 0) {
    parts.push(`ran the tests${d.tests > 1 ? ` ${d.tests} times` : ''}`)
  }
  // Commands are only worth mentioning on their own — "ran 12 commands" beside
  // "installed 3 packages" double-counts the same work in a reader's head.
  if (parts.length === 0 && d.commands > 0) {
    parts.push(`ran ${count(d.commands, 'command')}`)
  }
  return parts
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

/** Join phrases the way a person would say them out loud. */
export function sentence(parts: string[]): string {
  const joined =
    parts.length === 1
      ? parts[0]
      : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
  return `Claude ${joined}.`
}

function trim(text: string): string {
  const clean = text.trim()
  return clean.length > 220 ? `${clean.slice(0, 220)}…` : clean
}
