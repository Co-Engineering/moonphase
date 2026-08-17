import { createClient, type SupabaseClient } from '@supabase/supabase-js'
import type { InstanceConfig } from './host'

/**
 * The auth client, configured at runtime rather than at build time.
 *
 * Where auth lives is a property of the host you connected to, not of the
 * bundle. A phone installs the app once and is then told which server to talk
 * to, so the client is created after `GET /api/config` answers — see
 * `configure` below, called during boot before anything renders.
 */
let instance: SupabaseClient | null = null

export function configure(config: InstanceConfig): SupabaseClient {
  instance = createClient(config.supabase_url, config.supabase_anon_key, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      // The desktop shell has no address bar to strip a token out of, and we
      // never use the implicit OAuth flow, so URL detection only causes noise.
      detectSessionInUrl: false,
      // Namespaced by host: pointing the app at a different server must not
      // resurrect a session belonging to the previous one.
      storageKey: `moonphase.auth.${new URL(config.supabase_url).host}`,
    },
  })
  return instance
}

export function client(): SupabaseClient {
  if (!instance) {
    throw new Error('The Moonphase host has not been configured yet.')
  }
  return instance
}

export function isConfigured(): boolean {
  return instance !== null
}

export async function accessToken(): Promise<string | null> {
  if (!instance) return null
  const { data } = await instance.auth.getSession()
  return data.session?.access_token ?? null
}
