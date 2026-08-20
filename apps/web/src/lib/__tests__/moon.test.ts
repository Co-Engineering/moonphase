import { describe, expect, it } from 'vitest'
import { litPath, moonAt } from '../moon'

/**
 * The mark shows the real phase, so this has to be right rather than moon-ish.
 *
 * Checked against lunations that are a matter of record, not only against the
 * epoch it counts from — which would only prove it can subtract.
 */
describe('the phase of the moon', () => {
  it('is new at the epoch it counts from', () => {
    const moon = moonAt(Date.UTC(2000, 0, 6, 18, 14))
    expect(moon.illuminated).toBeLessThan(0.001)
    expect(moon.name).toBe('New moon')
  })

  it('agrees with a new moon two hundred lunations later', () => {
    // 11 January 2024, 11:57 UTC.
    const moon = moonAt(Date.UTC(2024, 0, 11, 11, 57))
    expect(moon.illuminated).toBeLessThan(0.01)
    expect(moon.name).toBe('New moon')
  })

  it('agrees with the full moon a fortnight after it', () => {
    // 25 January 2024, 17:54 UTC.
    const moon = moonAt(Date.UTC(2024, 0, 25, 17, 54))
    expect(moon.illuminated).toBeGreaterThan(0.99)
    expect(moon.name).toBe('Full moon')
  })

  it('knows which way it is going', () => {
    expect(moonAt(Date.UTC(2024, 0, 18)).waxing).toBe(true)
    expect(moonAt(Date.UTC(2024, 1, 1)).waxing).toBe(false)
  })

  it('is about half lit at a quarter, within what a mean month can promise', () => {
    // 18 January 2024, 03:53 UTC — first quarter.
    //
    // The tolerance is the point of this test. A mean synodic month cannot land
    // on the true quarter, because the moon's orbit is elliptical and it runs
    // early or late by up to half a day; here it reads 44% rather than 50%,
    // which is that lag and not a bug. Demanding better would be demanding the
    // full lunar theory, for a difference of a few pixels on a crescent.
    const first = moonAt(Date.UTC(2024, 0, 18, 3, 53))
    expect(first.illuminated).toBeGreaterThan(0.4)
    expect(first.illuminated).toBeLessThan(0.6)
    expect(first.waxing).toBe(true)
  })

  it('stays inside one cycle, including before the epoch', () => {
    for (const time of [
      Date.UTC(1969, 6, 20),
      Date.UTC(1999, 11, 31),
      Date.UTC(2030, 5, 5),
    ]) {
      const moon = moonAt(time)
      expect(moon.fraction).toBeGreaterThanOrEqual(0)
      expect(moon.fraction).toBeLessThan(1)
    }
  })
})

/**
 * The shape, which is where a phase goes wrong in a way the arithmetic does
 * not catch: a gibbous with its terminator bulging the wrong way is a
 * crescent, and both are perfectly valid path data.
 */
interface Arcs {
  /** Half-width of the terminator, in path units. */
  terminator: number
  /** Sweep flag of the limb, then of the terminator. */
  sweeps: [number, number]
}

function arcs(path: string): Arcs {
  const found = [...path.matchAll(/A ([\d.]+),\d+ 0 0 (\d)/g)]
  expect(found).toHaveLength(2)
  return {
    terminator: Number(found[1][1]),
    sweeps: [Number(found[0][2]), Number(found[1][2])],
  }
}

describe('the shape it draws', () => {
  it('puts the terminator where the geometry says', () => {
    // The edge between light and dark is a circle seen at an angle, so its
    // width across the disc is the cosine of how far through the cycle we are.
    for (const fraction of [0, 0.125, 0.25, 0.375, 0.5, 0.75, 0.9]) {
      const expected = Math.abs(Math.cos(2 * Math.PI * fraction)) * 100
      expect(arcs(litPath(fraction)).terminator).toBeCloseTo(expected, 1)
    }
  })

  it('draws a straight terminator at the quarters', () => {
    // An ellipse of zero width degenerates to the line down the middle.
    expect(arcs(litPath(0.25)).terminator).toBeCloseTo(0, 5)
    expect(arcs(litPath(0.75)).terminator).toBeCloseTo(0, 5)
  })

  it('lights nothing at new and everything at full', () => {
    // At both ends the terminator is as wide as the disc; what differs is
    // which way it curves — onto the limb, or away round the back.
    const New = arcs(litPath(0))
    const full = arcs(litPath(0.5))
    expect(New.terminator).toBeCloseTo(100, 1)
    expect(full.terminator).toBeCloseTo(100, 1)
    // New retraces the limb it just drew, enclosing nothing.
    expect(New.sweeps[1]).not.toBe(New.sweeps[0])
    // Full carries on round the other side, enclosing the disc.
    expect(full.sweeps[1]).toBe(full.sweeps[0])
  })

  it('bulges towards the lit limb when crescent, away when gibbous', () => {
    const crescent = arcs(litPath(0.125))
    const gibbous = arcs(litPath(0.375))
    // Same lit limb — both are waxing — and opposite terminators, which is the
    // entire difference between the two shapes.
    expect(crescent.sweeps[0]).toBe(gibbous.sweeps[0])
    expect(crescent.sweeps[1]).not.toBe(gibbous.sweeps[1])
  })

  it('lights the other limb on the way down', () => {
    expect(arcs(litPath(0.125)).sweeps[0]).not.toBe(
      arcs(litPath(0.875)).sweeps[0],
    )
  })
})
