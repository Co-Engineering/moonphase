import { describe, expect, it, vi } from 'vitest'
import { SHIFT_ENTER_SEQUENCE, handleShiftEnterKeydown, isShiftEnter } from '../Terminal'

function fakeEvent(overrides: Partial<{ key: string; shiftKey: boolean; type: string }> = {}) {
  return {
    key: 'Enter',
    shiftKey: true,
    type: 'keydown',
    ...overrides,
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
  }
}

/**
 * Enter and Shift+Enter arrive from xterm as the same key ("Enter") with only
 * `shiftKey` telling them apart, so this is the one thing that must never
 * misfire: get it wrong and either newlines stop working, or every plain
 * Enter starts inserting one instead of sending the message.
 */
describe('isShiftEnter', () => {
  it('is true for Shift+Enter', () => {
    expect(isShiftEnter({ key: 'Enter', shiftKey: true })).toBe(true)
  })

  it('is false for plain Enter', () => {
    expect(isShiftEnter({ key: 'Enter', shiftKey: false })).toBe(false)
  })

  it('is false for Shift+anything-else', () => {
    expect(isShiftEnter({ key: 'a', shiftKey: true })).toBe(false)
  })
})

describe('SHIFT_ENTER_SEQUENCE', () => {
  it('is ESC followed by carriage return', () => {
    // This is the exact sequence VS Code, iTerm2, Zed and Alacritty send for
    // their own native Shift+Enter bindings — not a value to rederive by
    // guessing, since the harness's keypress parser only recognises this one.
    expect(SHIFT_ENTER_SEQUENCE).toBe('\x1b\r')
  })
})

/**
 * `attachCustomKeyEventHandler` returning `false` tells xterm to skip its
 * own handling, but — unlike xterm's own handling — that alone does not call
 * `preventDefault`. Without it, the browser's native "insert a newline into
 * the focused textarea" default action for Enter still fires right behind
 * whatever this sends, which is exactly the shipped regression this guards:
 * Shift+Enter looked handled (the right bytes went out) but the terminal
 * still behaved like a plain Enter had been pressed too.
 */
describe('handleShiftEnterKeydown', () => {
  it('prevents the default action and stops propagation for Shift+Enter', () => {
    const event = fakeEvent()
    handleShiftEnterKeydown(event, { readOnly: false, send: vi.fn() })
    expect(event.preventDefault).toHaveBeenCalledTimes(1)
    expect(event.stopPropagation).toHaveBeenCalledTimes(1)
  })

  it('sends exactly the Shift+Enter sequence and nothing else', () => {
    const send = vi.fn()
    handleShiftEnterKeydown(fakeEvent(), { readOnly: false, send })
    expect(send).toHaveBeenCalledTimes(1)
    const [bytes] = send.mock.calls[0]
    expect(new TextDecoder().decode(bytes)).toBe(SHIFT_ENTER_SEQUENCE)
  })

  it('returns false, telling xterm not to do its own handling', () => {
    expect(handleShiftEnterKeydown(fakeEvent(), { readOnly: false, send: vi.fn() })).toBe(false)
  })

  it('leaves plain Enter alone entirely', () => {
    const event = fakeEvent({ shiftKey: false })
    const send = vi.fn()
    expect(handleShiftEnterKeydown(event, { readOnly: false, send })).toBe(true)
    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(event.stopPropagation).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
  })

  it('leaves non-keydown key events (e.g. keyup) alone', () => {
    const event = fakeEvent({ type: 'keyup' })
    expect(handleShiftEnterKeydown(event, { readOnly: false, send: vi.fn() })).toBe(true)
    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('still prevents the default action when read-only, so the leaked newline is blocked too', () => {
    const event = fakeEvent()
    const onRefused = vi.fn()
    const send = vi.fn()
    expect(handleShiftEnterKeydown(event, { readOnly: true, onRefused, send })).toBe(false)
    expect(event.preventDefault).toHaveBeenCalledTimes(1)
    expect(onRefused).toHaveBeenCalledTimes(1)
    expect(send).not.toHaveBeenCalled()
  })
})
