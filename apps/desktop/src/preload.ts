/**
 * The only privileged surface the renderer gets.
 *
 * Everything else in Moonphase is a plain web app talking to the API over HTTP,
 * and that is worth preserving — the same UI runs in a browser and on a phone.
 * The one thing a browser cannot do for itself is route its own requests
 * through a proxy, which is what a preview needs, so that single capability is
 * exposed here and nothing else.
 */
import { contextBridge, ipcRenderer } from 'electron'

export interface PreviewRequest {
  projectId: string
  projectName: string
  /**
   * Where the API is, and who is asking. The proxy runs on the API's machine,
   * which is not this one, so the shell opens a local port that carries each
   * connection there over an authenticated WebSocket.
   */
  apiUrl: string
  token: string
  /** Where to start, in the container's own terms — e.g. http://localhost:5173 */
  url: string
}

export interface SessionWindowRequest {
  projectId: string
  session: string
  title: string
  url: string
}

contextBridge.exposeInMainWorld('moonphase', {
  /** True when running inside the desktop shell, so the web build can adapt. */
  desktop: true,
  openPreview: (request: PreviewRequest): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('preview:open', request),
  openSessionWindow: (
    request: SessionWindowRequest,
  ): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('session:open', request),
})
