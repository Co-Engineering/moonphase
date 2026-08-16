import { api } from './api'

/**
 * Web push enrolment.
 *
 * Three things must line up before a notification can arrive — a registered
 * service worker, browser permission, and a subscription the backend knows
 * about — and any one of them failing is silent. So each step reports what
 * went wrong rather than returning a bare false.
 */

export type PushSupport =
  | { supported: true }
  | { supported: false; reason: string }

export function pushSupport(): PushSupport {
  if (!('serviceWorker' in navigator)) {
    return { supported: false, reason: 'This browser has no service worker support.' }
  }
  if (!('PushManager' in window)) {
    return { supported: false, reason: 'This browser has no push support.' }
  }
  if (!window.isSecureContext) {
    // The usual cause: reaching a remote backend over plain http. Worth saying
    // outright, because "nothing happens" is otherwise baffling.
    return {
      supported: false,
      reason: 'Push needs a secure context — serve the app over HTTPS or localhost.',
    }
  }
  return { supported: true }
}

/**
 * The applicationServerKey must be raw bytes, not the base64url string.
 *
 * Returned as an ArrayBuffer rather than a Uint8Array: the DOM types require a
 * view backed by a plain ArrayBuffer, and a bare Uint8Array is typed as
 * possibly backed by a SharedArrayBuffer.
 */
function decodeKey(base64url: string): ArrayBuffer {
  const padding = '='.repeat((4 - (base64url.length % 4)) % 4)
  const base64 = (base64url + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(base64)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return bytes.buffer
}

export async function registerWorker(): Promise<ServiceWorkerRegistration> {
  return navigator.serviceWorker.register('/sw.js', { scope: '/' })
}

function serialise(subscription: PushSubscription) {
  const json = subscription.toJSON()
  const keys = json.keys ?? {}
  if (!keys.p256dh || !keys.auth) {
    throw new Error('The browser returned a subscription without encryption keys.')
  }
  return {
    endpoint: subscription.endpoint,
    p256dh: keys.p256dh,
    auth: keys.auth,
    user_agent: navigator.userAgent.slice(0, 200),
  }
}

export async function enable(publicKey: string): Promise<void> {
  const support = pushSupport()
  if (!support.supported) throw new Error(support.reason)

  const registration = await registerWorker()
  await navigator.serviceWorker.ready

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') {
    throw new Error(
      permission === 'denied'
        ? 'Notifications are blocked for this site. Allow them in your browser settings.'
        : 'Notification permission was dismissed.',
    )
  }

  // Reuse an existing subscription; subscribing twice with a different key
  // throws, which happens whenever the VAPID keypair has been regenerated.
  let subscription = await registration.pushManager.getSubscription()
  if (subscription) {
    await subscription.unsubscribe().catch(() => undefined)
  }
  subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: decodeKey(publicKey),
  })

  await api.subscribePush(serialise(subscription))
}

export async function disable(): Promise<void> {
  const registration = await navigator.serviceWorker.getRegistration('/')
  const subscription = await registration?.pushManager.getSubscription()
  if (!subscription) return
  await api.unsubscribePush(serialise(subscription))
  await subscription.unsubscribe().catch(() => undefined)
}
