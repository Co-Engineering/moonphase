import { useCallback, useEffect, useRef, useState } from 'react'

interface State<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/**
 * Minimal async data hook.
 *
 * Deliberately not a caching library: the app has a handful of endpoints and
 * an explicit `reload()` after each mutation is easier to reason about than
 * invalidation keys.
 */
export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  options: { pollMs?: number } = {},
) {
  const [state, setState] = useState<State<T>>({
    data: null,
    error: null,
    loading: true,
  })

  // Guards against a slow first request resolving after a newer one and
  // overwriting fresher data.
  const generation = useRef(0)
  const mounted = useRef(true)

  const load = useCallback(
    async (quiet = false) => {
      const mine = ++generation.current
      if (!quiet) setState((s) => ({ ...s, loading: true }))
      try {
        const data = await fetcher()
        if (!mounted.current || mine !== generation.current) return
        setState({ data, error: null, loading: false })
      } catch (err) {
        if (!mounted.current || mine !== generation.current) return
        // Keep whatever we last had. Discarding it meant one failed poll —
        // a token refresh, a busy server, a dropped packet — emptied the
        // project list, which unmounted the open project and destroyed the
        // terminal attached to it. The next poll brought it back, so it read
        // as the app randomly losing your session mid-keystroke.
        setState((current) => ({
          data: current.data,
          error: err instanceof Error ? err.message : String(err),
          loading: false,
        }))
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  )

  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (!options.pollMs) return
    const id = window.setInterval(() => void load(true), options.pollMs)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.pollMs, ...deps])

  return { ...state, reload: load }
}
