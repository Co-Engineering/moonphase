import { describe, expect, it } from 'vitest'

import { formatBytes } from '../bytes'

describe('formatBytes', () => {
  it('handles zero and negative as 0 B', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(-5)).toBe('0 B')
  })

  it('formats bytes with no decimal', () => {
    expect(formatBytes(512)).toBe('512 B')
  })

  it('formats kilobytes', () => {
    expect(formatBytes(1_500)).toBe('1.50 KB')
  })

  it('formats megabytes under 10 with two decimals', () => {
    expect(formatBytes(6_041_000)).toBe('6.04 MB')
  })

  it('formats megabytes over 10 with one decimal', () => {
    expect(formatBytes(60_410_000)).toBe('60.4 MB')
  })

  it('formats gigabytes with one decimal once double digits', () => {
    expect(formatBytes(74_184_519_680)).toBe('74.2 GB')
  })

  it('formats terabytes', () => {
    expect(formatBytes(2_000_000_000_000)).toBe('2.00 TB')
  })
})
