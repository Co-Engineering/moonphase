import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export interface RowAction {
  label: string
  onSelect: () => void
  /** Styled as destructive, and asked about before it happens. */
  danger?: boolean
  /** Shown instead of the action when it cannot be done, and says why. */
  disabledReason?: string
  /** Second line, for what an action will actually do. */
  detail?: string
}

/**
 * The actions for one row in the sidebar.
 *
 * Removing a server, a project or a session was possible all along and lived at
 * the bottom of a detail panel you had to know to open — so people reported
 * that it could not be done. Sharing had the same problem from the other
 * direction: a button that only exists on a screen you have already navigated
 * into is a button most people never meet.
 *
 * So the actions sit on the thing they act on. Same menu, same order, same
 * place for all three kinds, because the point is that you stop having to
 * remember where each one keeps its buttons.
 *
 * Destructive actions ask first, in the menu rather than through a browser
 * dialog: `confirm()` is easy to dismiss by reflex and says nothing about what
 * is about to be lost.
 *
 * The menu itself is placed against the viewport rather than against the row.
 * The sidebar scrolls, and a scrolling box clips what overflows it — so a menu
 * opened on a row near the bottom was cut off, taking the last item with it.
 * The last item is the destructive one.
 */
export function RowMenu({
  label,
  actions,
}: {
  label: string
  actions: RowAction[]
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)
  const container = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const items = useRef<HTMLDivElement>(null)
  const [place, setPlace] = useState<{
    right: number
    top?: number
    bottom?: number
  } | null>(null)

  // Measured before the browser paints, so the menu never shows up in the
  // wrong place first. Re-run when `confirming` changes, because the menu
  // grows when it asks and a taller menu may no longer fit below.
  useLayoutEffect(() => {
    if (!open) {
      setPlace(null)
      return
    }
    const anchor = trigger.current?.getBoundingClientRect()
    const height = items.current?.offsetHeight ?? 0
    if (!anchor) return

    const gap = 8
    const room = window.innerHeight - anchor.bottom - gap
    setPlace({
      // Right-aligned to the trigger, which is how it sat when it was
      // positioned against the row.
      right: Math.max(gap, window.innerWidth - anchor.right),
      // Below when it fits, above when it does not.
      ...(height && room < height
        ? { bottom: window.innerHeight - anchor.top + 4 }
        : { top: anchor.bottom + 4 }),
    })
  }, [open, confirming])

  useEffect(() => {
    if (!open) return
    // Fixed to the viewport, so scrolling the sidebar would leave the menu
    // hanging beside a row that has moved. Close instead of chasing it.
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
      if (!container.current?.contains(event.target as Node)) {
        setOpen(false)
        setConfirming(null)
      }
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false)
        setConfirming(null)
      }
    }
    // Capture, so a click anywhere closes this before that click does whatever
    // else it was going to do underneath.
    document.addEventListener('mousedown', dismiss, true)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', dismiss, true)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  if (actions.length === 0) return null

  return (
    <div className="row-menu" ref={container}>
      <button
        ref={trigger}
        className="row-menu-open"
        aria-label={`Actions for ${label}`}
        aria-expanded={open}
        title={`Actions for ${label}`}
        onClick={(event) => {
          // The row underneath navigates; this button does not.
          event.stopPropagation()
          setOpen((was) => !was)
          setConfirming(null)
        }}
      >
        ⋯
      </button>

      {open && (
        <div
          className="row-menu-items"
          role="menu"
          ref={items}
          style={{
            ...place,
            // Hidden only for the first frame, before it has been measured.
            // Laid out either way, so there is a height to measure.
            visibility: place ? 'visible' : 'hidden',
          }}
        >
          {actions.map((action) =>
            action.disabledReason ? (
              <span className="row-menu-item disabled" key={action.label}>
                {action.label}
                <span className="hint">{action.disabledReason}</span>
              </span>
            ) : confirming === action.label ? (
              <span className="row-menu-item confirming" key={action.label}>
                <span className="hint">{action.detail ?? 'This cannot be undone.'}</span>
                <span className="row-menu-confirm">
                  <button
                    className="danger"
                    onClick={(event) => {
                      event.stopPropagation()
                      setOpen(false)
                      setConfirming(null)
                      action.onSelect()
                    }}
                  >
                    {action.label}
                  </button>
                  <button
                    onClick={(event) => {
                      event.stopPropagation()
                      setConfirming(null)
                    }}
                  >
                    Cancel
                  </button>
                </span>
              </span>
            ) : (
              <button
                className={`row-menu-item${action.danger ? ' danger' : ''}`}
                role="menuitem"
                key={action.label}
                onClick={(event) => {
                  event.stopPropagation()
                  if (action.danger) {
                    setConfirming(action.label)
                    return
                  }
                  setOpen(false)
                  action.onSelect()
                }}
              >
                {action.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  )
}
