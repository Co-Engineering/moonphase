/**
 * Moonphase desktop shell.
 *
 * Intentionally thin: it renders the same frontend the browser and phone use,
 * so there is exactly one UI to maintain. The window is a convenience, not a
 * privileged client — no sessions live here, and closing it detaches rather
 * than stopping anything.
 */
import { app, BrowserWindow, shell } from 'electron'
import * as path from 'node:path'

// Set by `pnpm dev`; in a packaged build we load the built assets from disk.
const DEV_SERVER_URL = process.env.MOONPHASE_DEV_SERVER_URL ?? 'http://127.0.0.1:5273'
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

void app.whenReady().then(() => {
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
