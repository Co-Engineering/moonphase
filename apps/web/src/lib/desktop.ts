/**
 * The desktop shell's one extra capability.
 *
 * A previewed app asks for the addresses it was written with —
 * `http://localhost:8000` and the like — and those only mean the right thing if
 * the browser resolves them inside the container. Only a client whose proxy we
 * can set does that, which in practice means the Electron window.
 *
 * Everywhere else this is absent and the caller falls back to a forwarded port,
 * which works for a single-service app and cannot work for one that calls its
 * own API by name. Better to say so than to open something half-broken.
 */
import { currentHost } from './host'
import { accessToken } from './supabase'

export interface PreviewRequest {
  projectId: string
  projectName: string
  /**
   * Where the API is, and who is asking.
   *
   * The proxy runs on the API's machine and listens on its loopback, which for
   * an installed app is not this machine at all — so the shell opens a local
   * port of its own and carries each connection to the API over an
   * authenticated WebSocket. It needs the address and a token to do that.
   */
  apiUrl: string
  token: string
  url: string
}

export interface SessionWindowRequest {
  projectId: string
  session: string
  title: string
  url: string
}

interface DesktopBridge {
  desktop: true
  openPreview: (request: PreviewRequest) => Promise<{ ok: boolean; error?: string }>
  openSessionWindow: (
    request: SessionWindowRequest,
  ) => Promise<{ ok: boolean; error?: string }>
}

declare global {
  interface Window {
    moonphase?: DesktopBridge
  }
}

export const isDesktop = (): boolean => Boolean(window.moonphase?.desktop)

/**
 * Put one session in its own OS window.
 *
 * People run several agents at once, and the answer to laying those out is not
 * an in-app tiling scheme — it is real windows, which a tiling window manager
 * already arranges better than we could, across as many monitors as there are.
 * In a browser this is a plain popup, which works the same way.
 */
export async function openSessionWindow(request: SessionWindowRequest): Promise<void> {
  const bridge = window.moonphase
  if (bridge) {
    const result = await bridge.openSessionWindow(request)
    if (!result.ok) throw new Error(result.error ?? 'Could not open the window.')
    return
  }
  const opened = window.open(
    request.url,
    `moonphase:${request.projectId}:${request.session}`,
    'width=1000,height=760',
  )
  if (!opened) throw new Error('The browser blocked the popup.')
}

export function sessionWindowUrl(projectId: string, session: string): string {
  const params = new URLSearchParams({
    window: 'session',
    project: projectId,
    session,
  })
  return `${window.location.origin}${window.location.pathname}?${params}`
}

export async function openPreviewWindow(
  request: Omit<PreviewRequest, 'apiUrl' | 'token'>,
): Promise<void> {
  const bridge = window.moonphase
  if (!bridge) throw new Error('Preview windows are only available in the desktop app.')

  const token = await accessToken()
  if (!token) throw new Error('You are signed out. Sign in and try again.')

  const result = await bridge.openPreview({
    ...request,
    apiUrl: currentHost(),
    token,
  })
  if (!result.ok) throw new Error(result.error ?? 'Could not open the preview.')
}
