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
export interface PreviewRequest {
  projectId: string
  projectName: string
  proxyPort: number
  url: string
}

interface DesktopBridge {
  desktop: true
  openPreview: (request: PreviewRequest) => Promise<{ ok: boolean; error?: string }>
}

declare global {
  interface Window {
    moonphase?: DesktopBridge
  }
}

export const isDesktop = (): boolean => Boolean(window.moonphase?.desktop)

export async function openPreviewWindow(request: PreviewRequest): Promise<void> {
  const bridge = window.moonphase
  if (!bridge) throw new Error('Preview windows are only available in the desktop app.')
  const result = await bridge.openPreview(request)
  if (!result.ok) throw new Error(result.error ?? 'Could not open the preview.')
}
