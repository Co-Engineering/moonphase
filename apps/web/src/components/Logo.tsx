import { useEffect, useState } from 'react'
import { litPath, moonAt } from '../lib/moon'

/**
 * The Moonphase mark, showing the moon that is actually up.
 *
 * The app is named for a phase, so it may as well be the real one: a crescent
 * tonight, a full disc in a fortnight. Nothing depends on it — it is the same
 * mark either way — but a logo that quietly tracks the sky is a better joke
 * than a logo that draws one.
 *
 * The unlit part is always drawn, faintly. Without it the mark would thin to
 * nothing for the two days around new moon, and a brand that disappears once a
 * month is a poor trade for accuracy nobody asked for.
 *
 * `currentColor` throughout, so it takes the accent in the sidebar and the text
 * colour on a sign-in card without needing a second copy.
 */
export function Logo({ size = 16 }: { size?: number }) {
  const [moon, setMoon] = useState(() => moonAt())

  // Anyone who leaves this open for days should see it move. Hourly is far more
  // often than the shape visibly changes, and costs nothing.
  useEffect(() => {
    const id = window.setInterval(() => setMoon(moonAt()), 60 * 60 * 1000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <svg
      className="logo"
      width={size}
      height={size}
      viewBox="-115 -115 230 230"
      role="img"
      aria-label={`Moonphase — ${moon.name.toLowerCase()}`}
      focusable="false"
    >
      <title>{moon.name}</title>
      <circle r="100" fill="currentColor" opacity="0.22" />
      <path d={litPath(moon.fraction)} fill="currentColor" />
    </svg>
  )
}
