import { useEffect, useRef, useState } from 'react'

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
        <div className="row-menu-items" role="menu">
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
