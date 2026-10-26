import { useMemo } from 'react'
import {
  api,
  type HarnessAccount,
  type HarnessInfo,
  type HarnessKind,
  type Project,
  type Session,
} from './api'
import { useResource } from './useResource'

type ByHarness = Partial<Record<HarnessKind, HarnessAccount[]>>

/**
 * Your accounts, harness by harness, for the harnesses your projects and sessions run.
 *
 * An account belongs to one harness, and the backend refuses one that is not
 * the session's harness's with "No such account.". Each project or session
 * gets `accountsFor(target)`, its own harness's list.
 *
 * Claude Code's are always loaded; another harness's once a project or
 * session here uses it, or once it is signed in to — a new session can be
 * started on any of those, and its account picker needs the list.
 */
export function useHarnessAccounts(
  projects: Pick<Project, 'harness'>[] | null | undefined,
  sessions?: Pick<Session, 'harness'>[] | null | undefined,
  harnesses?: Pick<HarnessInfo, 'kind' | 'configured'>[] | null | undefined,
) {
  const kinds = useMemo(() => {
    const found = new Set<HarnessKind>(['claude_code'])
    for (const project of projects ?? []) if (project.harness) found.add(project.harness)
    for (const session of sessions ?? []) if (session.harness) found.add(session.harness)
    for (const harness of harnesses ?? []) if (harness.configured) found.add(harness.kind as HarnessKind)
    return [...found].sort()
  }, [projects, sessions, harnesses])
  const key = kinds.join(',')

  // Each harness's list on its own: one that fails to load leaves the others
  // in place, rather than every project's picker going empty with it.
  const resource = useResource<ByHarness>(async () => {
    const settled = await Promise.allSettled(kinds.map((kind) => api.harnessAccounts(kind)))
    const loaded: ByHarness = {}
    settled.forEach((result, i) => {
      if (result.status === 'fulfilled') loaded[kinds[i]] = result.value
    })
    const failed = settled.find((result) => result.status === 'rejected')
    if (failed && Object.keys(loaded).length === 0) throw failed.reason
    return loaded
  }, [key])

  return {
    ...resource,
    /** A project or session's own harness's accounts; none until they have loaded. */
    accountsFor: (target: Pick<Project, 'harness'> | HarnessKind | string): HarnessAccount[] => {
      const kind = typeof target === 'string' ? target : (target.harness ?? 'claude_code')
      return resource.data?.[kind as HarnessKind] ?? []
    },
  }
}
