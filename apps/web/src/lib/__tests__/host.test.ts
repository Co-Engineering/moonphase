import { describe, expect, it, beforeEach } from 'vitest'
import {
  currentHost,
  forgetHost,
  insecureHostWarning,
  normaliseHost,
  rememberHost,
} from '../host'

describe('normaliseHost', () => {
  it('assumes https, because notifications need it', () => {
    // Typing a bare hostname is what people do, and defaulting to http would
    // quietly cost them push and installation with no explanation.
    expect(normaliseHost('moonphase.example.com')).toBe('https://moonphase.example.com')
  })

  it('leaves an explicit scheme alone', () => {
    expect(normaliseHost('http://192.168.1.10:8471')).toBe('http://192.168.1.10:8471')
    expect(normaliseHost('https://moon.dev')).toBe('https://moon.dev')
  })

  it('strips trailing slashes, which are pasted constantly', () => {
    // Every request appends a path; a doubled slash 404s on some proxies.
    expect(normaliseHost('https://moon.dev/')).toBe('https://moon.dev')
    expect(normaliseHost('https://moon.dev///')).toBe('https://moon.dev')
  })

  it('treats blank as blank rather than inventing a host', () => {
    expect(normaliseHost('   ')).toBe('')
  })
})

describe('currentHost', () => {
  beforeEach(() => forgetHost())

  it('prefers what the user chose', () => {
    rememberHost('https://chosen.example')
    expect(currentHost()).toBe('https://chosen.example')
  })

  it('falls back to something rather than nothing', () => {
    // A fresh install served by the API needs no setup at all, so this must
    // never come back empty and strand the app on a blank screen.
    expect(currentHost()).toBeTruthy()
  })
})

describe('insecureHostWarning', () => {
  it('warns about plain http on a home network', () => {
    // The single most likely way to end up with no notifications and no
    // explanation, so it has to be said before connecting, not after.
    expect(insecureHostWarning('http://192.168.1.10:8471')).toContain('HTTPS')
  })

  it('says nothing about https', () => {
    expect(insecureHostWarning('https://moon.dev')).toBeNull()
  })

  it('says nothing about localhost, which browsers exempt', () => {
    expect(insecureHostWarning('http://localhost:8471')).toBeNull()
    expect(insecureHostWarning('http://127.0.0.1:8471')).toBeNull()
  })
})
