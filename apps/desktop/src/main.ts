/**
 * Moonphase desktop shell.
 *
 * Intentionally thin: it renders the same frontend the browser and phone use,
 * so there is exactly one UI to maintain. The window is a convenience, not a
 * privileged client — no sessions live here, and closing it detaches rather
 * than stopping anything.
 */
import { app, BrowserWindow, ipcMain, session, shell } from 'electron'
import * as path from 'node:path'

// Set by `pnpm dev`; in a packaged build we load the built assets from disk.
const DEV_SERVER_URL = process.env.MOONPHASE_DEV_SERVER_URL ?? 'http://127.0.0.1:8472'
const isDev = process.env.NODE_ENV !== 'production' && !app.isPackaged

let window: BrowserWindow | null = null

function createWindow(): void {
  window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: '#0b0c12',
    title: 'Moonphase',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      // The renderer is a plain web app talking to the API over HTTP and
      // WebSocket. It has no need for Node, and granting it any would widen
      // the blast radius of anything the agent renders into the terminal.
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })

  if (isDev) {
    void window.loadURL(DEV_SERVER_URL)
  } else {
    void window.loadFile(path.join(__dirname, '../../web/dist/index.html'))
  }

  // Anything the app tries to open in a new window is an external link —
  // hand it to the real browser instead of spawning a chromeless window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  window.on('closed', () => {
    window = null
  })
}

/**
 * A preview window whose network sits inside the project container.
 *
 * This is the whole point. A page served from the container runs in a browser
 * *here*, so when its code asks for `http://localhost:8000` it gets this
 * machine's port 8000 — not the API it means. Forwarding cannot fix that,
 * because the address is the application's choice and it asks for the one it
 * was written with.
 *
 * Routing the window through the project's SOCKS proxy changes what those
 * names mean instead: every address resolves in the container, so hardcoded
 * ports, absolute URLs, websockets and CORS all behave exactly as they would
 * if the code were running on this machine.
 */
async function openPreview(request: {
  projectId: string
  projectName: string
  proxyPort: number
  url: string
}): Promise<{ ok: boolean; error?: string }> {
  const existing = previews.get(request.projectId)
  if (existing && !existing.isDestroyed()) {
    existing.focus()
    void existing.loadURL(request.url)
    return { ok: true }
  }

  // Partitioned so the proxy applies to this project and nothing else —
  // including Moonphase's own window, which must keep talking to the API
  // directly.
  const partition = `preview:${request.projectId}`
  const previewSession = session.fromPartition(partition)

  try {
    await previewSession.setProxy({
      proxyRules: `socks5://127.0.0.1:${request.proxyPort}`,
      // The load-bearing line. Chromium never proxies loopback addresses by
      // default, so without this every `localhost` request — which is to say
      // all of them — would go straight to this machine and miss the container
      // entirely. `<-loopback>` removes that implicit exception.
      proxyBypassRules: '<-loopback>',
    })
  } catch (error) {
    return { ok: false, error: String(error) }
  }

  const preview = new BrowserWindow({
    width: 1100,
    height: 800,
    backgroundColor: '#ffffff',
    title: `${request.projectName} — preview`,
    webPreferences: {
      partition,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  })

  previews.set(request.projectId, preview)
  preview.on('closed', () => previews.delete(request.projectId))
  // Links inside a previewed app open in the same window: it is the app's own
  // navigation, and sending it to the real browser would drop it off the proxy
  // and back onto this machine's localhost.
  preview.webContents.setWindowOpenHandler(({ url }) => {
    void preview.loadURL(url)
    return { action: 'deny' }
  })

  await preview.loadURL(request.url)
  return { ok: true }
}

const previews = new Map<string, BrowserWindow>()

void app.whenReady().then(() => {
  ipcMain.handle('preview:open', (_event, request) => openPreview(request))
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
