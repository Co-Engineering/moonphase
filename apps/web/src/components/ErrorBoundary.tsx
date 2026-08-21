import { Component, type ErrorInfo, type ReactNode } from 'react'
import { copyText } from '../lib/clipboard'

/**
 * Something to look at when the app breaks.
 *
 * React 18 unmounts the entire tree when a render throws and nothing catches
 * it. The window goes empty — not an error, not a stack trace, just nothing —
 * and the only way to find out what happened is to have had the console open
 * at the moment it went. Three separate bugs have presented this way, and each
 * time the symptom was identical and useless: a blank screen.
 *
 * So the fix is not to prevent it, because you cannot prevent every bug. It is
 * to make it say something. A boundary turns an uncaught error into a message
 * with the error in it, which is the difference between "it went blank again"
 * and a report someone can act on.
 *
 * Deliberately dependency-free and styled with the app's own tokens, because
 * this is the one component that has to work when something else has already
 * failed.
 */

interface Props {
  children: ReactNode
  /**
   * What broke, in the user's terms. A boundary around one project view can
   * say so, which tells them the rest of the app is still fine.
   */
  what?: string
  /**
   * Reset without a full reload where that makes sense — remounting a project
   * view is cheaper and less destructive than reloading the window.
   */
  onReset?: () => void
}

interface State {
  error: Error | null
  stack: string
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept on the instance rather than only in the console: whoever hits this
    // will not have had devtools open, and asking them to reproduce it with
    // the console up is asking them to hit it twice.
    this.setState({ stack: info.componentStack ?? '' })
    console.error('[moonphase] uncaught error', error, info.componentStack)
  }

  private reset = (): void => {
    this.setState({ error: null, stack: '' })
    this.props.onReset?.()
  }

  render(): ReactNode {
    const { error, stack } = this.state
    if (!error) return this.props.children

    const report = [
      `${error.name}: ${error.message}`,
      error.stack ?? '',
      stack ? `\nComponent stack:${stack}` : '',
    ]
      .filter(Boolean)
      .join('\n')

    return (
      <div className="crash">
        <div className="crash-inner">
          <h2>{this.props.what ?? 'Moonphase'} stopped working</h2>
          <p className="hint">
            Nothing on your servers is affected — the sessions are still running, and
            this window is the only thing that broke.
          </p>

          <pre className="crash-detail">{report}</pre>

          <div className="actions">
            <button className="primary" onClick={this.reset}>
              Try again
            </button>
            <button className="ghost" onClick={() => window.location.reload()}>
              Reload
            </button>
            <button
              className="ghost"
              onClick={() => void copyText(report)}
            >
              Copy details
            </button>
          </div>
        </div>
      </div>
    )
  }
}

/**
 * Errors that never reach React.
 *
 * A boundary catches renders and lifecycles, and nothing else — a rejected
 * promise in a timer is invisible to it. Those do not blank the window, but
 * they are the other half of "why did it behave strangely", so they are worth
 * having in the console with a marker you can grep for.
 */
export function reportUncaught(): void {
  window.addEventListener('error', (event) => {
    console.error('[moonphase] uncaught', event.error ?? event.message)
  })
  window.addEventListener('unhandledrejection', (event) => {
    console.error('[moonphase] unhandled rejection', event.reason)
  })
}
