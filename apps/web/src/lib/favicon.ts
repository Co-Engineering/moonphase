import { litPath, moonAt } from './moon'

/**
 * Point the browser tab at tonight's moon.
 *
 * The static `icon.svg` is a fixed crescent, because a file on disk cannot know
 * what day it is — and the icons a launcher or a home screen shows are baked at
 * install time, so those stay fixed too. A tab is different: the page is
 * running, so it can say.
 *
 * Drawn on the app's own dark ground rather than in `currentColor`, because a
 * tab strip is whatever colour the browser feels like and a transparent
 * crescent is invisible against half of them.
 */
const GROUND = '#12141d'
const ACCENT = '#7aa2f7'

function svg(fraction: number): string {
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">' +
    `<rect width="512" height="512" rx="112" fill="${GROUND}"/>` +
    '<g transform="translate(256 256) scale(1.6)">' +
    `<circle r="100" fill="${ACCENT}" opacity="0.22"/>` +
    `<path d="${litPath(fraction)}" fill="${ACCENT}"/>` +
    '</g></svg>'
  )
}

/** Set it now, and again every hour for anyone who never closes the tab. */
export function trackMoonInFavicon(): () => void {
  const paint = (): void => {
    const link =
      document.querySelector<HTMLLinkElement>('link[rel="icon"]') ??
      document.head.appendChild(
        Object.assign(document.createElement('link'), { rel: 'icon' }),
      )
    link.type = 'image/svg+xml'
    // A data URI rather than a blob: no object to revoke, and no lifetime to
    // get wrong on a document that outlives every other object here.
    link.href = `data:image/svg+xml,${encodeURIComponent(svg(moonAt().fraction))}`
  }

  paint()
  const id = window.setInterval(paint, 60 * 60 * 1000)
  return () => window.clearInterval(id)
}
