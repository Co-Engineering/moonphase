/**
 * Moonphase service worker.
 *
 * Exists for one reason: receive a push when no page is open. That is the
 * whole point of the notification — if the app were on screen the user would
 * already know.
 */

self.addEventListener('install', () => {
  // Take over immediately; waiting for a reload would mean the first
  // subscription of a session silently receives nothing.
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('push', (event) => {
  let payload = {}
  try {
    payload = event.data ? event.data.json() : {}
  } catch {
    payload = { title: 'Moonphase', body: event.data ? event.data.text() : '' }
  }

  event.waitUntil(
    Promise.all([
      self.registration.showNotification(payload.title || 'Moonphase', {
        body: payload.body || '',
        // Same tag replaces rather than stacks, so a chatty project cannot
        // bury everything else in the shade.
        tag: payload.tag || 'moonphase',
        renotify: true,
        badge: '/icon-192.png',
        icon: '/icon-192.png',
        // A question needs answering, so it should survive the screen being
        // ignored rather than disappearing on its own.
        requireInteraction: payload.kind === 'awaiting_input',
        vibrate: [80, 40, 80],
        data: { url: payload.url || '/' },
      }),
      // The count on the home screen icon. Reads as a real app, and survives
      // the notification being swiped away — which is exactly when you are
      // most likely to forget something was waiting.
      countWaiting(),
    ]),
  )
})

/**
 * How many notifications are still on screen, as the icon badge.
 *
 * Derived from the shade rather than counted in the worker, because a service
 * worker is stopped and restarted at the browser's discretion and any total it
 * kept would be wrong by morning.
 */
async function countWaiting() {
  if (!self.navigator || !self.navigator.setAppBadge) return
  try {
    const shown = await self.registration.getNotifications()
    if (shown.length > 0) await self.navigator.setAppBadge(shown.length)
    else await self.navigator.clearAppBadge()
  } catch {
    // Unsupported or denied. The notification itself still arrived.
  }
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = (event.notification.data && event.notification.data.url) || '/'

  // Focus an existing window rather than opening a second copy of the app.
  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then(async (clients) => {
        await countWaiting()
        for (const client of clients) {
          if ('focus' in client) {
            client.postMessage({ type: 'navigate', url: target })
            return client.focus()
          }
        }
        return self.clients.openWindow(target)
      }),
  )
})
