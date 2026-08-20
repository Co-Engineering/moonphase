/**
 * Moonphase desktop shell.
 *
 * Intentionally thin: it renders the same frontend the browser and phone use,
 * so there is exactly one UI to maintain. The window is a convenience, not a
 * privileged client — no sessions live here, and closing it detaches rather
 * than stopping anything.
 */
import { app, BrowserWindow, ipcMain, session, shell } from 'electron'
import { existsSync } from 'node:fs'
import * as path from 'node:path'
import { closeAllRelays, closeRelay, ensureRelay } from './socksrelay'

// Set by `pnpm dev`; in a packaged build we load the built assets from disk.
const DEV_SERVER_URL = process.env.MOONPHASE_DEV_SERVER_URL ?? 'http://127.0.0.1:8472'
const isDev = process.env.NODE_ENV !== 'production' && !app.isPackaged

/**
 * Where the built frontend ended up.
 *
 * Two layouts, because there are two ways to arrive here. Packaging copies the
 * web build in beside this one, so it travels inside the app; a checkout builds
 * it in its own workspace and leaves it there. Guessing wrong shows an empty
 * window with nothing in the log, so both are tried and the failure names the
 * places it looked.
 */
function frontendEntry(): string {
  const candidates = [
    path.join(__dirname, '..', 'web', 'index.html'),
    path.join(__dirname, '..', '..', 'web', 'dist', 'index.html'),
  ]
  const found = candidates.find((candidate) => existsSync(candidate))
  if (found) return found
  throw new Error(
    `Could not find the built frontend. Looked in:\n  ${candidates.join('\n  ')}\n` +
      'Run `pnpm build` at the repository root.',
  )
}

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
    void window.loadFile(frontendEntry())
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
/**
 * The renderer is our own app, but it renders whatever an agent writes into a
 * terminal, and this is the one call that can point a window at an arbitrary
 * address. Both arguments are therefore checked rather than trusted.
 */
function validate(request: { url: string }): string | null {
  let parsed: URL
  try {
    parsed = new URL(request.url)
  } catch {
    return 'Invalid preview URL.'
  }
  // http(s) only. `file:` would read the local disk into a window the proxy
  // does not even apply to, and the other schemes have no business here.
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return `Refusing to preview a ${parsed.protocol} URL.`
  }
  return null
}

async function openPreview(request: {
  projectId: string
  projectName: string
  apiUrl: string
  token: string
  url: string
}): Promise<{ ok: boolean; error?: string }> {
  const invalid = validate(request)
  if (invalid) return { ok: false, error: invalid }

  // The proxy runs wherever the API does, which for an installed app is not
  // this machine. A local port that carries each connection there is what makes
  // the window's proxy setting mean something.
  let proxyPort: number
  try {
    proxyPort = await ensureRelay({
      apiUrl: request.apiUrl,
      projectId: request.projectId,
      token: request.token,
    })
  } catch (error) {
    return { ok: false, error: String(error) }
  }

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
      proxyRules: `socks5://127.0.0.1:${proxyPort}`,
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
  preview.on('closed', () => {
    previews.delete(request.projectId)
    // The relay exists for this window. Leaving it listening would keep a port
    // open, and a WebSocket's worth of server-side proxy, for a preview nobody
    // has open any more.
    closeRelay({ apiUrl: request.apiUrl, projectId: request.projectId })
  })
  // Links inside a previewed app open in the same window: it is the app's own
  // navigation, and sending it to the real browser would drop it off the proxy
  // and back onto this machine's localhost.
  preview.webContents.setWindowOpenHandler(({ url }) => {
    // The app's own navigation stays in this window, on the proxy. Sending it
    // to the real browser would drop it back onto this machine's localhost,
    // where it means something else entirely.
    if (validate({ url }) === null) {
      void preview.loadURL(url)
    }
    return { action: 'deny' }
  })

  await preview.loadURL(request.url)
  return { ok: true }
}

const previews = new Map<string, BrowserWindow>()
const sessionWindows = new Map<string, BrowserWindow>()

/**
 * One session in a window of its own.
 *
 * People run several agents at once and want to see them at the same time.
 * Laying that out is a problem an operating system already solves better than
 * an in-app pane splitter ever will: a tiling window manager arranges these
 * across as many monitors as there are, and a plain one lets you drag them
 * wherever you like. So Moonphase makes windows and gets out of the way.
 *
 * No proxy here — this is Moonphase's own UI, talking to the API as usual.
 */
async function openSessionWindow(request: {
  projectId: string
  session: string
  title: string
  url: string
}): Promise<{ ok: boolean; error?: string }> {
  const invalid = validate({ url: request.url })
  if (invalid) return { ok: false, error: invalid }

  const key = `${request.projectId}:${request.session}`
  const existing = sessionWindows.get(key)
  if (existing && !existing.isDestroyed()) {
    // Raising the one that exists, rather than making a second view of the
    // same session, which would attach twice and squeeze the window.
    if (existing.isMinimized()) existing.restore()
    existing.focus()
    return { ok: true }
  }

  const window = new BrowserWindow({
    width: 900,
    height: 720,
    minWidth: 480,
    minHeight: 320,
    backgroundColor: '#0b0c12',
    title: request.title,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })
  sessionWindows.set(key, window)
  window.on('closed', () => sessionWindows.delete(key))
  await window.loadURL(request.url)
  return { ok: true }
}

void app.whenReady().then(() => {
  ipcMain.handle('preview:open', (_event, request) => openPreview(request))
  ipcMain.handle('session:open', (_event, request) => openSessionWindow(request))
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

// On macOS the app stays alive with no windows, so relays are closed here
// rather than above: a listening port must not outlive the app on any platform.
app.on('will-quit', () => {
  closeAllRelays()
})
