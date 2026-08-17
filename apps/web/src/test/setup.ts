import '@testing-library/jest-dom/vitest'

/**
 * A working localStorage.
 *
 * Node 22 exposes a global `localStorage` that throws unless the process was
 * started with `--localstorage-file`, and it shadows the one jsdom provides.
 * Every read in this codebase is wrapped in try/catch — losing a preference
 * must never break the app — so the failure is silent and a test would quietly
 * assert against the fallback path instead of the one it meant to check.
 */
const store = new Map<string, string>()

Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, String(value)),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => [...store.keys()][index] ?? null,
    get length() {
      return store.size
    },
  },
})
