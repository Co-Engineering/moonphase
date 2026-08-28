import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { GitHubRepo } from '../lib/api'

interface Props {
  value: string
  onChange: (url: string) => void
  repos: GitHubRepo[] | null
  loading: boolean
  error: string | null
}

/**
 * Repository picker for New Project.
 *
 * `repos` is `null` when GitHub is not connected (or the list has not loaded
 * yet) — in that case this renders the same plain URL input the form always
 * had, so a public repository someone doesn't own is still just a paste
 * away. Once repos are available, "Other — paste a URL" stays in the list
 * for exactly that case, rather than replacing the picker outright.
 */
export function RepoPicker({ value, onChange, repos, loading, error }: Props) {
  const [manual, setManual] = useState(false)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const container = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  // Where to draw the list. It is placed against the viewport rather than the
  // field, because the dialog it sits in scrolls — and a scrolling box clips
  // what leaves it. This is the last field in that dialog, so the list was cut
  // off part way down its second row and the repositories below it could not
  // be clicked at all.
  const [place, setPlace] = useState<{
    left: number
    width: number
    top?: number
    bottom?: number
  } | null>(null)

  const reposition = useCallback(() => {
    const anchor = field.current?.getBoundingClientRect()
    if (!anchor) return
    const gap = 8
    // The list's own maximum, so the decision does not depend on how many
    // repositories happen to have loaded.
    const height = 240
    const room = window.innerHeight - anchor.bottom - gap
    setPlace({
      left: anchor.left,
      width: anchor.width,
      ...(room < height
        ? { bottom: window.innerHeight - anchor.top + 4 }
        : { top: anchor.bottom + 4 }),
    })
  }, [])

  useLayoutEffect(() => {
    if (!open) {
      setPlace(null)
      return
    }
    reposition()
  }, [open, query, reposition])

  useEffect(() => {
    if (!open) return
    // Fixed to the viewport, so it has to track the field rather than sit
    // where it was measured — both a scroll and a resize can move or resize
    // the field without the list following on its own.
    //
    // This used to close the list outright instead of tracking it, which
    // broke picking a repo on a phone entirely: focusing this field (the
    // last one in a dialog that scrolls) makes the browser scroll it above
    // the keyboard, and the keyboard opening resizes the viewport — both
    // fire right as the picker opens, closing it before a single character
    // could be typed. The field's own displayed value depends on `open`, so
    // from there every keystroke looked like it did nothing.
    window.addEventListener('scroll', reposition, true)
    window.addEventListener('resize', reposition)
    return () => {
      window.removeEventListener('scroll', reposition, true)
      window.removeEventListener('resize', reposition)
    }
  }, [open, reposition])

  useEffect(() => {
    if (!open) return
    const dismiss = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', dismiss, true)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', dismiss, true)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  if (repos === null || error) {
    return (
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="https://github.com/you/private-repo.git"
      />
    )
  }

  if (manual) {
    return (
      <div className="repo-picker">
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="https://github.com/you/private-repo.git"
          autoFocus
        />
        <button
          type="button"
          className="repo-picker-back"
          onClick={() => setManual(false)}
        >
          ← Choose from your repositories
        </button>
      </div>
    )
  }

  const selected = repos.find((r) => r.clone_url === value)
  const filtered = repos.filter((r) =>
    r.full_name.toLowerCase().includes(query.toLowerCase()),
  )

  const pick = (repo: GitHubRepo) => {
    onChange(repo.clone_url)
    setQuery('')
    setOpen(false)
  }

  return (
    <div className="repo-picker" ref={container}>
      <input
        ref={field}
        value={open ? query : selected?.full_name ?? ''}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => {
          setQuery('')
          setOpen(true)
        }}
        placeholder={loading ? 'Loading your repositories…' : 'Search your repositories…'}
        disabled={loading}
      />
      {open && (
        <div
          className="repo-picker-list"
          role="listbox"
          style={{
            ...place,
            // Laid out but unseen until it has been measured, so it never
            // appears in the wrong place first.
            visibility: place ? 'visible' : 'hidden',
          }}
        >
          {filtered.length === 0 && (
            <div className="repo-picker-item disabled">No matching repositories</div>
          )}
          {filtered.map((repo) => (
            <button
              type="button"
              key={repo.full_name}
              className="repo-picker-item"
              role="option"
              onClick={() => pick(repo)}
            >
              {repo.full_name}
              {repo.private && <span className="hint"> · private</span>}
            </button>
          ))}
          <button
            type="button"
            className="repo-picker-item repo-picker-other"
            onClick={() => {
              setOpen(false)
              setManual(true)
            }}
          >
            Other — paste a URL
          </button>
        </div>
      )}
    </div>
  )
}
