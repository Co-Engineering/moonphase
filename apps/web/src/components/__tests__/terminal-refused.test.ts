import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * Telling a refused handshake apart from a dropped connection.
 *
 * A WebSocket the server refuses during the handshake closes with a bare
 * 1006 and no reason: the rejection happens before the socket is accepted,
 * so there is nowhere to put one. That is indistinguishable, at the
 * `onclose` callback, from the network going away — and the message shown
 * for it said "connection lost", which points at the user's network.
 *
 * v0.9.0's Origin regression refused every socket on every reverse-proxied
 * deployment, and this sentence is all anyone saw. The one fact that
 * separates the two cases is whether the socket ever opened.
 *
 * Read out of the source: the branch lives inside a closure over a live
 * WebSocket and an xterm instance, and what matters is that the distinction
 * is drawn at all, from the right fact.
 */
const source = readFileSync(
  resolve(process.cwd(), 'src/components/Terminal.tsx'),
  'utf8',
)

describe('a socket that closes without ever having opened', () => {
  it('tracks whether the socket ever opened', () => {
    expect(source).toMatch(/let everOpened = false/)
    expect(source).toMatch(/everOpened = true/)
  })

  it('says the server refused it, rather than blaming a lost connection', () => {
    expect(source).toContain('the server refused this connection')
  })

  it('still calls a genuine drop a lost connection', () => {
    expect(source).toContain('connection lost')
  })

  it('chooses between the two on whether the socket opened', () => {
    const closeHandler = source.slice(
      source.indexOf('socket.onclose'),
      source.indexOf('socket.onerror'),
    )
    expect(closeHandler).toMatch(/everOpened\s*$/m)
    expect(closeHandler).toContain('connection lost')
    expect(closeHandler).toContain('the server refused this connection')
  })

  it('keeps retrying either way — a restarting server also refuses', () => {
    const closeHandler = source.slice(
      source.indexOf('socket.onclose'),
      source.indexOf('socket.onerror'),
    )
    expect(closeHandler).toContain('reconnectTimer = window.setTimeout(connect, delay)')
  })
})
