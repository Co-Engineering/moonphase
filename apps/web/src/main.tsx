import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { ErrorBoundary, reportUncaught } from './components/ErrorBoundary'
import { trackMoonInFavicon } from './lib/favicon'
import './styles.css'

const root = document.getElementById('root')
if (!root) throw new Error('#root is missing from index.html')

reportUncaught()
trackMoonInFavicon()

createRoot(root).render(
  <StrictMode>
    {/* Outermost, so a crash shows something rather than emptying the window.
        There is a second boundary around the project view, which keeps the
        sidebar usable when only one session is the problem. */}
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
