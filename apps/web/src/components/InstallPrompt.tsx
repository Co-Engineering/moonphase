import { useEffect, useState } from 'react'
import { isApplePhone, isInstalled } from '../lib/notifications'

/**
 * The event Chromium fires when a site is installable. Not in lib.dom yet.
 */
interface InstallEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/**
 * Getting Moonphase onto a home screen.
 *
 * Worth its own affordance because on an iPhone it is not optional: Safari
 * exposes no push at all until a site has been installed, so "add to home
 * screen" is the difference between notifications working and the feature
 * appearing not to exist. Chromium will offer to do it for us; Safari has no
 * API for it at all, so there the only honest thing is to describe the taps.
 */
export function InstallPrompt() {
  const [event, setEvent] = useState<InstallEvent | null>(null)
  const [installed, setInstalled] = useState(isInstalled())

  useEffect(() => {
    const onPrompt = (e: Event) => {
      // Chromium shows its own banner otherwise, at a moment of its choosing
      // rather than when someone is reading about notifications.
      e.preventDefault()
      setEvent(e as InstallEvent)
    }
    const onInstalled = () => setInstalled(true)
    window.addEventListener('beforeinstallprompt', onPrompt)
    window.addEventListener('appinstalled', onInstalled)
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt)
      window.removeEventListener('appinstalled', onInstalled)
    }
  }, [])

  if (installed) return null

  if (event) {
    return (
      <div className="banner info install-banner">
        <span>Install Moonphase for notifications that behave like any other app.</span>
        <button
          className="primary"
          onClick={() => {
            void event.prompt().then(() => setEvent(null))
          }}
        >
          Install
        </button>
      </div>
    )
  }

  if (isApplePhone()) {
    return (
      <div className="banner info">
        <strong>On iPhone and iPad, install it first.</strong>
        <br />
        Notifications only work from an installed app: tap Share, then{' '}
        <em>Add to Home Screen</em>, and open Moonphase from there. Needs iOS 16.4 or
        later.
      </div>
    )
  }

  return null
}
