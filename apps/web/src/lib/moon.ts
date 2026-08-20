/**
 * What the moon is actually doing, right now.
 *
 * The app is named for a phase, so the mark shows the real one: a crescent
 * tonight, a full disc in a fortnight. It is decoration, but it is decoration
 * that is true, which is a different thing from a picture of a moon.
 *
 * The maths is the standard mean-synodic approximation: count synodic months
 * from a known new moon. The real moon runs up to about half a day either side
 * of that, because its orbit is elliptical and the sun keeps pulling on it —
 * which matters to an almanac and not at all to a shape a centimetre across.
 * Anything better would mean the full lunar theory for a difference nobody can
 * see.
 */

/** Days between one new moon and the next, on average. */
const SYNODIC_MONTH = 29.530588853

/**
 * A new moon everyone agrees on: 2000-01-06 18:14 UTC.
 *
 * Written as a timestamp rather than a Date so it is a constant rather than
 * something parsed differently by whichever engine is running.
 */
const KNOWN_NEW_MOON = Date.UTC(2000, 0, 6, 18, 14) // ms

const DAY = 86_400_000

export interface Moon {
  /**
   * Where we are in the cycle: 0 is new, 0.25 first quarter, 0.5 full, 0.75
   * last quarter. Always in [0, 1).
   */
  fraction: number
  /** How much of the disc is lit, 0 to 1. */
  illuminated: number
  /** Growing towards full, rather than shrinking towards new. */
  waxing: boolean
  /** What you would call it out loud. */
  name: string
}

const NAMES = [
  'New moon',
  'Waxing crescent',
  'First quarter',
  'Waxing gibbous',
  'Full moon',
  'Waning gibbous',
  'Last quarter',
  'Waning crescent',
]

/** Name the phase the way people do — the quarters are moments, so they get a
 * narrow band around them rather than an eighth of the month each. */
function nameFor(fraction: number): string {
  const eighth = 1 / 8
  // Within a day and a half of an exact quarter, call it that.
  const edge = 1.5 / SYNODIC_MONTH
  for (let i = 0; i < 4; i += 1) {
    const exact = i * 0.25
    const distance = Math.min(
      Math.abs(fraction - exact),
      Math.abs(fraction - exact - 1),
    )
    if (distance < edge) return NAMES[i * 2]
  }
  const index = Math.floor(fraction / eighth + 0.5) % 8
  return NAMES[index]
}

export function moonAt(when: Date | number = Date.now()): Moon {
  const time = typeof when === 'number' ? when : when.getTime()
  const cycles = (time - KNOWN_NEW_MOON) / DAY / SYNODIC_MONTH
  // Positive remainder, so dates before the epoch behave.
  const fraction = ((cycles % 1) + 1) % 1
  return {
    fraction,
    illuminated: (1 - Math.cos(2 * Math.PI * fraction)) / 2,
    waxing: fraction < 0.5,
    name: nameFor(fraction),
  }
}

/**
 * The lit part of the disc, as SVG path data on a circle of radius `r` centred
 * on the origin.
 *
 * A phase is a sphere lit from the side, so the edge between light and dark is
 * a circle seen at an angle — an ellipse, always as tall as the moon and as
 * wide as the cosine of how far through the cycle we are. Which is why this is
 * two arcs and not a circle with a bite out of it: a bite gives you a crescent
 * and nothing else, and the moon spends most of the month not being one.
 */
export function litPath(fraction: number, r = 100): string {
  const angle = 2 * Math.PI * fraction
  const k = Math.abs(Math.cos(angle)) * r
  const gibbous = fraction > 0.25 && fraction < 0.75
  const waxing = fraction < 0.5

  // Lit limb on the right while waxing, on the left while waning.
  const side = waxing ? 1 : 0
  // The terminator bulges away from the lit limb when gibbous and towards it
  // when crescent, which is the whole difference between the two shapes. Get it
  // backwards and the path is still valid — it just draws the opposite month.
  const bulge = gibbous ? side : 1 - side

  return (
    `M 0,${-r} ` +
    `A ${r},${r} 0 0 ${side} 0,${r} ` +
    `A ${k.toFixed(2)},${r} 0 0 ${bulge} 0,${-r} Z`
  )
}
