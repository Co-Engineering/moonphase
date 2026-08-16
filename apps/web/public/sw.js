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
    self.registration.showNotification(payload.title || 'Moonphase', {
      body: payload.body || '',
      // Same tag replaces rather than stacks, so a chatty project cannot
      // bury everything else in the shade.
      tag: payload.tag || 'moonphase',
      renotify: true,
      badge: '/icon.png',
      icon: '/icon.png',
      data: { url: payload.url || '/' },
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = (event.notification.data && event.notification.data.url) || '/'

  // Focus an existing window rather than opening a second copy of the app.
  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((clients) => {
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
