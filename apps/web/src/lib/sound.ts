/**
 * A notification sound, synthesized rather than shipped as an audio file —
 * nothing to source or license, and it's a handful of milliseconds of a sine
 * wave either way.
 */

export type SoundChoice = 'chime' | 'ping'

export const SOUND_CHOICES: SoundChoice[] = ['chime', 'ping']

// Lazily created and reused: browsers cap how many AudioContexts can exist,
// and creating one before any user interaction can throw in some of them.
let ctx: AudioContext | null = null

function context(): AudioContext | null {
  if (typeof window === 'undefined') return null
  const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctor) return null
  if (!ctx) ctx = new Ctor()
  return ctx
}

function tone(audio: AudioContext, when: number, freq: number, duration: number, gain = 0.2) {
  const osc = audio.createOscillator()
  const g = audio.createGain()
  osc.type = 'sine'
  osc.frequency.value = freq
  // A hard onset clicks; ramping up over 10ms and back down exponentially
  // is the standard way to make a synthesized blip sound like a chime
  // instead of a click.
  g.gain.setValueAtTime(0, when)
  g.gain.linearRampToValueAtTime(gain, when + 0.01)
  g.gain.exponentialRampToValueAtTime(0.001, when + duration)
  osc.connect(g)
  g.connect(audio.destination)
  osc.start(when)
  osc.stop(when + duration + 0.02)
}

export function playAlertSound(choice: SoundChoice): void {
  const audio = context()
  if (!audio) return
  // Suspended (autoplay policy, or created before any user gesture) just
  // means silence rather than an error — resume() is safe either way.
  void audio.resume()
  const now = audio.currentTime
  if (choice === 'ping') {
    tone(audio, now, 1046.5, 0.16)
    return
  }
  tone(audio, now, 880, 0.18)
  tone(audio, now + 0.12, 1318.5, 0.22)
}

export function soundChoiceLabel(choice: SoundChoice): string {
  return choice === 'ping' ? 'Ping' : 'Chime'
}
