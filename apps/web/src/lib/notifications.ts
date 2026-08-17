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
  | { supported: false; reason: string; fix?: string }

/** Running from the home screen rather than in a browser tab. */
export function isInstalled(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // Safari's own, predating the standard and still the only one it sets.
    (navigator as { standalone?: boolean }).standalone === true
  )
}

/** iPhone, iPad — including iPadOS, which claims to be a Mac with a touchscreen. */
export function isApplePhone(): boolean {
  const ua = navigator.userAgent
  if (/iphone|ipod|ipad/i.test(ua)) return true
  return /macintosh/i.test(ua) && navigator.maxTouchPoints > 1
}

/**
 * Whether a notification can arrive on this device, and if not, what to do.
 *
 * The reason matters more than the verdict. On an iPhone, Safari exposes no
 * PushManager at all until the site has been added to the Home Screen, so the
 * honest report — "this browser has no push support" — is both true and
 * useless: the browser does support it, once installed. Anyone reading that
 * would conclude Moonphase does not work on their phone and stop.
 */
export function pushSupport(): PushSupport {
  if (!window.isSecureContext) {
    // Almost always a phone pointed at a plain-http address on a home network.
    return {
      supported: false,
      reason: 'Notifications need a secure connection.',
      fix:
        'Serve Moonphase over HTTPS — a reverse proxy, or a Tailscale or ' +
        'Cloudflare tunnel. Browsers allow this on localhost only.',
    }
  }
  if (!('serviceWorker' in navigator)) {
    return { supported: false, reason: 'This browser has no service worker support.' }
  }
  if (!('PushManager' in window)) {
    if (isApplePhone() && !isInstalled()) {
      return {
        supported: false,
        reason: 'Add Moonphase to your Home Screen first.',
        fix:
          'On iPhone and iPad, notifications only work from an installed app. ' +
          'Tap Share, then "Add to Home Screen", open it from there, and come ' +
          'back to this screen. Needs iOS 16.4 or later.',
      }
    }
    if (isApplePhone()) {
      return {
        supported: false,
        reason: 'This version of iOS cannot receive web notifications.',
        fix: 'Web push arrived in iOS 16.4.',
      }
    }
    return { supported: false, reason: 'This browser has no push support.' }
  }
  return { supported: true }
}

/**
 * The count on the app icon, which is the part that makes it feel like an app
 * rather than a bookmark. Supported on installed apps on Android and on iOS
 * 16.4+; a no-op everywhere else, which is why nothing checks first.
 */
export async function setBadge(count: number): Promise<void> {
  const nav = navigator as {
    setAppBadge?: (n?: number) => Promise<void>
    clearAppBadge?: () => Promise<void>
  }
  try {
    if (count > 0) await nav.setAppBadge?.(count)
    else await nav.clearAppBadge?.()
  } catch {
    // Unsupported, or denied. The notification itself still arrives.
  }
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
