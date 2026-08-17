/**
 * Which Moonphase host this client talks to.
 *
 * Baking the address in at build time is fine for a desktop shell you compile
 * yourself and useless for a phone: you install the app once and it has to be
 * told where your server is. So the address is a runtime setting, and
 * everything else — where auth lives, which key signs push — is discovered
 * from `GET /api/config` on that host rather than configured a second time.
 *
 * The default is the origin the app was served from, which is right whenever
 * the API serves the frontend. Typing a URL is for the other case.
 */

const STORAGE_KEY = 'moonphase.host'

export interface InstanceConfig {
  supabase_url: string
  supabase_anon_key: string
  vapid_public_key: string | null
  version: string
}

/** Trailing slashes and a missing scheme are the two things people type. */
export function normaliseHost(input: string): string {
  const trimmed = input.trim().replace(/\/+$/, '')
  if (!trimmed) return ''
  if (!/^https?:\/\//i.test(trimmed)) return `https://${trimmed}`
  return trimmed
}

export function storedHost(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function rememberHost(host: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, host)
  } catch {
    // Private browsing. The app still works for this session.
  }
}

export function forgetHost(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing to do.
  }
}

/**
 * In order: what the user chose, what this build was compiled with, and the
 * origin we were served from. The last is the common case once the API serves
 * the frontend, and means a fresh install of the PWA needs no setup at all.
 */
export function currentHost(): string {
  const stored = storedHost()
  if (stored) return stored
  const built = import.meta.env.VITE_API_URL
  if (built) return normaliseHost(built)
  return window.location.origin
}

export async function fetchConfig(host: string): Promise<InstanceConfig> {
  const response = await fetch(`${host}/api/config`, {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(
      `${host} answered ${response.status}. Is that a Moonphase host?`,
    )
  }
  const config = (await response.json()) as InstanceConfig
  if (!config.supabase_url || !config.supabase_anon_key) {
    throw new Error(`${host} is a Moonphase host but has no auth configured.`)
  }
  return config
}

/**
 * Push and service workers need a secure context, and a phone pointed at a
 * plain-http address on a home network is the most likely way to end up
 * without one. Silence is the worst possible answer there, so this exists to
 * be said out loud.
 */
export function insecureHostWarning(host: string): string | null {
  if (window.isSecureContext) return null
  if (/^https:/i.test(host)) return null
  if (/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|$)/i.test(host)) return null
  return (
    'This host is plain HTTP, so the browser will not allow notifications or ' +
    'installing the app. Serve Moonphase over HTTPS — a reverse proxy, or a ' +
    'Tailscale or Cloudflare tunnel, all work.'
  )
}
