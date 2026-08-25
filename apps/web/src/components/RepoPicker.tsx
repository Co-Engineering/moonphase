import { useEffect, useLayoutEffect, useRef, useState } from 'react'
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

  useLayoutEffect(() => {
    if (!open) {
      setPlace(null)
      return
    }
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
  }, [open, query])

  useEffect(() => {
    if (!open) return
    // Fixed to the viewport, so scrolling the dialog would leave the list
    // beside a field that has moved.
    const leave = () => setOpen(false)
    window.addEventListener('scroll', leave, true)
    window.addEventListener('resize', leave)
    return () => {
      window.removeEventListener('scroll', leave, true)
      window.removeEventListener('resize', leave)
    }
  }, [open])

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
