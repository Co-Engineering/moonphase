import { afterEach, describe, expect, it, vi } from 'vitest'
import { playAlertSound } from '../sound'

class FakeGain {
  gain = {
    setValueAtTime: vi.fn(),
    linearRampToValueAtTime: vi.fn(),
    exponentialRampToValueAtTime: vi.fn(),
  }
  connect = vi.fn()
}

class FakeOscillator {
  type = ''
  frequency = { value: 0 }
  connect = vi.fn()
  start = vi.fn()
  stop = vi.fn()
}

class FakeAudioContext {
  currentTime = 0
  destination = {}
  resume = vi.fn(async () => {})
  createGain = vi.fn(() => new FakeGain())
  createOscillator = vi.fn(() => new FakeOscillator())
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('playAlertSound', () => {
  it('does nothing (and does not throw) when the Web Audio API is unavailable', () => {
    vi.stubGlobal('AudioContext', undefined)
    expect(() => playAlertSound('chime')).not.toThrow()
  })

  it('plays two tones for chime and one for ping', () => {
    const instances: FakeAudioContext[] = []
    vi.stubGlobal(
      'AudioContext',
      vi.fn(() => {
        const inst = new FakeAudioContext()
        instances.push(inst)
        return inst
      }),
    )

    playAlertSound('chime')
    expect(instances[0].createOscillator).toHaveBeenCalledTimes(2)

    playAlertSound('ping')
    // Same AudioContext instance is reused across calls.
    expect(instances[0].createOscillator).toHaveBeenCalledTimes(3)
  })
})
