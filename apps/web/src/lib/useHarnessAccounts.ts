import { useMemo } from 'react'
import { api, type HarnessAccount, type HarnessKind, type Project } from './api'
import { useResource } from './useResource'

type ByHarness = Partial<Record<HarnessKind, HarnessAccount[]>>

/**
 * Your accounts, harness by harness, for the harnesses your projects run.
 *
 * An account belongs to one harness, and the backend refuses one that is not
 * the project's harness's with "No such account.". The app used to load a
 * single list — Claude's — and hand it to every project, so an Antigravity
 * project's pickers offered Claude accounts that could only ever fail. Each
 * project now gets `accountsFor(project)`, its own harness's list.
 *
 * Claude Code's are always loaded, exactly as before; another harness's once
 * a project here uses it. The list of harnesses only changes when a project
 * on a new one appears, so the projects poll does not refetch accounts.
 */
export function useHarnessAccounts(projects: Pick<Project, 'harness'>[] | null | undefined) {
  const kinds = useMemo(() => {
    const found = new Set<HarnessKind>(['claude_code'])
    for (const project of projects ?? []) if (project.harness) found.add(project.harness)
    return [...found].sort()
  }, [projects])
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
    /** A project's own harness's accounts; none until they have loaded. */
    accountsFor: (project: Pick<Project, 'harness'>): HarnessAccount[] =>
      resource.data?.[project.harness ?? 'claude_code'] ?? [],
  }
}
