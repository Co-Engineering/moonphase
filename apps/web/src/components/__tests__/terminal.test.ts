import { describe, expect, it } from 'vitest'
import { SHIFT_ENTER_SEQUENCE, isShiftEnter } from '../Terminal'

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
