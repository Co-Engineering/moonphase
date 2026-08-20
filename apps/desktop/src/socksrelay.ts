/**
 * A local SOCKS port for a project whose proxy lives on the server.
 *
 * The preview window works by pointing a browser at a SOCKS proxy where every
 * address resolves inside the project's container, so `localhost:8000` means
 * what the code running in there means by it. The API runs that proxy, bound to
 * its own loopback and published nowhere — it is an unauthenticated path *as
 * the container*, and exposing it would hand anyone who reached the port a way
 * in.
 *
 * While the desktop shell only existed as a development build beside the API,
 * that loopback was also the browser's, and the address worked by coincidence.
 * An installed app talking to a server across the internet has its own
 * `127.0.0.1`, which contains no proxy at all — the window failed to connect to
 * something running perfectly well several hundred miles away.
 *
 * So this listens where the browser can reach it, and carries each connection
 * to the API over a WebSocket that authenticates as the person using the app
 * and is checked against their access to the project. Nothing is published, the
 * bytes are the same SOCKS conversation either way, and this end never has to
 * understand a single one of them.
 */
import { createServer, type Server, type Socket } from 'node:net'
import WebSocket from 'ws'

interface RelayKey {
  apiUrl: string
  projectId: string
}

interface Relay {
  server: Server
  port: number
  apiUrl: string
  token: string
}

const relays = new Map<string, Relay>()

const keyOf = ({ apiUrl, projectId }: RelayKey): string => `${apiUrl}|${projectId}`

/** `https://host` → `wss://host`, so one address configures both. */
function socketUrl(apiUrl: string, projectId: string, token: string): string {
  const url = new URL(`/ws/projects/${projectId}/preview/socks`, apiUrl)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  // The token also goes in the Authorization header below, which is where the
  // API reads it from. It is repeated here only because a proxy or gateway in
  // front may strip headers it does not recognise on an upgrade request, and a
  // preview that fails for that reason is indistinguishable from one that is
  // broken.
  url.searchParams.set('token', token)
  return url.toString()
}

/**
 * Carry one accepted connection to the API and back.
 *
 * Deliberately byte-for-byte: this end parses no SOCKS, so there is no second
 * implementation of the protocol to disagree with the first.
 */
function bridge(socket: Socket, url: string, token: string): void {
  const websocket = new WebSocket(url, {
    headers: { Authorization: `Bearer ${token}` },
    // A preview loads dozens of resources at once and each one is its own
    // socket; keeping them small matters more than keeping them fast.
    perMessageDeflate: false,
  })

  // Anything the browser sends before the WebSocket is open would otherwise be
  // dropped: Chromium writes its SOCKS greeting the instant it connects.
  const pending: Buffer[] = []
  let open = false

  socket.on('data', (chunk: Buffer) => {
    if (open) websocket.send(chunk)
    else pending.push(chunk)
  })

  websocket.on('open', () => {
    open = true
    for (const chunk of pending) websocket.send(chunk)
    pending.length = 0
  })

  websocket.on('message', (data: WebSocket.RawData) => {
    // `ws` hands back a Buffer, an ArrayBuffer or an array of Buffers
    // depending on how the frame arrived.
    if (Buffer.isBuffer(data)) socket.write(data)
    else if (Array.isArray(data)) socket.write(Buffer.concat(data))
    else socket.write(Buffer.from(data as ArrayBuffer))
  })

  const shutdown = (): void => {
    if (websocket.readyState === WebSocket.OPEN) websocket.close()
    socket.destroy()
  }

  websocket.on('close', shutdown)
  websocket.on('error', shutdown)
  socket.on('close', shutdown)
  socket.on('error', shutdown)
}

/**
 * The local port for this project, starting a listener if there is not one.
 *
 * Bound to loopback, like the proxy it stands in for: any local process can
 * already reach the browser's own ports, and this adds no reach beyond them.
 * One per project so that closing a preview cannot disturb another.
 */
export async function ensureRelay(request: {
  apiUrl: string
  projectId: string
  token: string
}): Promise<number> {
  const key = keyOf(request)
  const existing = relays.get(key)
  if (existing) {
    // The token is refreshed periodically, and a relay outlives one. Keeping
    // the newest means a preview opened an hour later still authenticates.
    existing.token = request.token
    return existing.port
  }

  const relay: Relay = {
    server: createServer(),
    port: 0,
    apiUrl: request.apiUrl,
    token: request.token,
  }

  relay.server.on('connection', (socket) => {
    socket.setNoDelay(true)
    bridge(socket, socketUrl(relay.apiUrl, request.projectId, relay.token), relay.token)
  })

  await new Promise<void>((resolve, reject) => {
    relay.server.once('error', reject)
    relay.server.listen(0, '127.0.0.1', () => {
      relay.server.removeListener('error', reject)
      resolve()
    })
  })

  const address = relay.server.address()
  if (address === null || typeof address === 'string') {
    relay.server.close()
    throw new Error('Could not open a local port for the preview proxy.')
  }

  relay.port = address.port
  relays.set(key, relay)
  return relay.port
}

/** Stop the relay for a project, if it has one. */
export function closeRelay(request: RelayKey): void {
  const key = keyOf(request)
  const relay = relays.get(key)
  if (!relay) return
  relays.delete(key)
  relay.server.close()
}

/** Stop every relay. Called on the way out, so no listener outlives the app. */
export function closeAllRelays(): void {
  for (const relay of relays.values()) relay.server.close()
  relays.clear()
}
