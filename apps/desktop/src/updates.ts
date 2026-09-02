/**
 * Whether a newer Moonphase desktop build has been released than this one.
 *
 * Mirrors apps/api/moonphase/updates.py: `/releases/latest` ignores
 * pre-releases and drafts, so the rolling `edge` build the install scripts
 * ship never shows up as an update to itself — only a tagged release does.
 *
 * No auto-download here. The app is not code-signed, and Squirrel.Mac
 * refuses to drive an update onto an unsigned build, so the honest thing this
 * can do is say a release exists and hand the person its page.
 */
const REPO = 'Co-Engineering/moonphase'

// A release tag: v1.2.3, with an optional pre-release or build suffix.
const TAG = /^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/

function versionTuple(tag: string): [number, number, number] | null {
  const match = TAG.exec(tag.trim())
  if (!match) return null
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

/**
 * Whether `latest` is a later release than `running`.
 *
 * Compared as numbers, not as text: "v0.10.0" sorts before "v0.9.0" as a
 * string. Anything unparseable falls back to inequality, which errs towards
 * offering an update that changes nothing rather than hiding one that
 * matters.
 */
export function isNewer(latest: string, running: string): boolean {
  const left = versionTuple(latest)
  const right = versionTuple(running)
  if (!left || !right) return latest.trim() !== running.trim()
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index]
  }
  return false
}

export interface UpdateCheck {
  updateAvailable: boolean
  latestVersion: string | null
  releaseUrl: string | null
  /** Set when the question could not be answered — GitHub unreachable, or nothing published yet. */
  detail: string | null
}

/** Compares `running` (e.g. `app.getVersion()`) against the latest published release. */
export async function checkForUpdates(running: string): Promise<UpdateCheck> {
  const unknown = (detail: string): UpdateCheck => ({
    updateAvailable: false,
    latestVersion: null,
    releaseUrl: null,
    detail,
  })

  let response: Response
  try {
    response = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'moonphase' },
    })
  } catch (error) {
    return unknown(`Could not reach GitHub: ${String(error)}`)
  }

  if (response.status === 404) {
    return unknown('No releases have been published yet, so there is nothing to compare this against.')
  }
  if (!response.ok) {
    return unknown(`GitHub returned ${response.status} ${response.statusText}.`)
  }

  const release = (await response.json()) as { tag_name?: string; html_url?: string }
  const latest = (release.tag_name ?? '').trim()
  if (!latest) return unknown('The latest release has no version tag.')

  return {
    updateAvailable: isNewer(latest, running),
    latestVersion: latest,
    releaseUrl: release.html_url ?? null,
    detail: null,
  }
}
