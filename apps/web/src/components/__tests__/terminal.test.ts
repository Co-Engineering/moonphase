import { describe, expect, it, vi } from 'vitest'
import {
  HARNESS_CLIPBOARD_PASTE_TRIGGER,
  SHIFT_ENTER_SEQUENCE,
  clipboardImagePasteFollowUp,
  handleShiftEnterKeydown,
  isPlainPasteCombo,
  readClipboardImage,
  isShiftEnter,
} from '../Terminal'

function pasteCombo(
  overrides: Partial<{ key: string; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }> = {},
) {
  return { key: 'v', ctrlKey: true, metaKey: false, shiftKey: false, altKey: false, ...overrides }
}

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

describe('HARNESS_CLIPBOARD_PASTE_TRIGGER', () => {
  it('is Ctrl+V (0x16) — the only thing the harness’s own clipboard-image check listens for', () => {
    expect(HARNESS_CLIPBOARD_PASTE_TRIGGER).toBe('\x16')
  })
})

/**
 * The shipped regression this guards: pasting an image only worked via a
 * right-click paste or a drop, never the single most obvious gesture —
 * plain Ctrl+V (or Cmd+V) — because no browser paste event ever fires for
 * that specific, unshifted combo in this app's tested environments, and
 * xterm has no idea it should mean anything but the literal control
 * character it already sends on. Getting this predicate wrong either misses
 * the fix (plain Ctrl+V still does nothing) or, worse, hijacks a shifted or
 * plain-alt combo that was never broken to begin with.
 */
describe('isPlainPasteCombo', () => {
  it('is true for Ctrl+V', () => {
    expect(isPlainPasteCombo(pasteCombo({ ctrlKey: true }))).toBe(true)
  })

  /**
   * The one that has to stay false. xterm claims only Cmd+A on macOS and
   * leaves every other Cmd chord uncancelled, so the browser fires its own
   * paste event for Cmd+V and the onPaste handler already stages the image.
   * Intercepting it would preventDefault that event: the image would still
   * arrive, but pasted text would stop arriving and land as a stray 0x16.
   */
  it('is false for Cmd+V — macOS pastes it natively, and text would break', () => {
    expect(isPlainPasteCombo(pasteCombo({ ctrlKey: false, metaKey: true }))).toBe(false)
  })

  it('is false for Ctrl+Cmd+V', () => {
    expect(isPlainPasteCombo(pasteCombo({ ctrlKey: true, metaKey: true }))).toBe(false)
  })

  it('is false for Ctrl+Shift+V — already works via the browser’s own paste event', () => {
    expect(isPlainPasteCombo(pasteCombo({ shiftKey: true }))).toBe(false)
  })

  it('is false for Ctrl+Alt+V', () => {
    expect(isPlainPasteCombo(pasteCombo({ altKey: true }))).toBe(false)
  })

  it('is false for a bare V with no modifier', () => {
    expect(isPlainPasteCombo(pasteCombo({ ctrlKey: false }))).toBe(false)
  })

  it('is false for Ctrl+anything-else', () => {
    expect(isPlainPasteCombo(pasteCombo({ key: 'c' }))).toBe(false)
  })
})

/**
 * A pasted image was staged on the harness's side over the socket, but
 * nothing there makes the harness go looking for it — bracketed-paste text
 * (`term.paste`) is a different code path from the Ctrl+V keystroke the
 * harness's own clipboard-image check is bound to, so a staged file with no
 * follow-up trigger just sits there. This is the exact shipped regression:
 * "still not possible to paste an image" with no error, because the image
 * really was staged — it just was never asked for.
 */
describe('clipboardImagePasteFollowUp', () => {
  it('sends the trigger once staging succeeded', () => {
    expect(clipboardImagePasteFollowUp(true, '').sendTrigger).toBe(true)
  })

  it('does not send the trigger when staging failed — nothing there to find', () => {
    expect(clipboardImagePasteFollowUp(false, '').sendTrigger).toBe(false)
  })

  it('pastes any accompanying text regardless of whether staging succeeded', () => {
    expect(clipboardImagePasteFollowUp(true, 'a caption').pasteText).toBe('a caption')
    expect(clipboardImagePasteFollowUp(false, 'a caption').pasteText).toBe('a caption')
  })

  it('has nothing to paste for a pure image with no text', () => {
    expect(clipboardImagePasteFollowUp(true, '').pasteText).toBeNull()
  })
})

/**
 * A clipboard read that never settles.
 *
 * `navigator.clipboard.read()` stays *pending* while the browser's
 * clipboard-read prompt is open — it does not reject. Ctrl+V has already
 * been swallowed by preventDefault at that point, so awaiting it unbounded
 * means the keystroke does nothing at all until someone answers a prompt
 * they may not have noticed. It has to time out and let the keystroke
 * through as itself.
 */
describe('reading an image off the clipboard for Ctrl+V', () => {
  it('gives up rather than hanging on an unanswered permission prompt', async () => {
    const neverSettles = { read: () => new Promise<ClipboardItem[]>(() => {}) }
    const started = Date.now()
    const result = await readClipboardImage(neverSettles as unknown as Clipboard, 20)

    expect(result).toBeNull()
    expect(Date.now() - started).toBeLessThan(1000)
  })

  it('gives up when the read is refused outright', async () => {
    const refuses = { read: () => Promise.reject(new Error('NotAllowedError')) }
    expect(await readClipboardImage(refuses as unknown as Clipboard, 50)).toBeNull()
  })

  it('returns nothing when the clipboard holds no image', async () => {
    const textOnly = { read: async () => [{ types: ['text/plain'] }] }
    expect(await readClipboardImage(textOnly as unknown as Clipboard, 50)).toBeNull()
  })

  it('returns the image when there is one', async () => {
    const png = new Blob(['x'], { type: 'image/png' })
    const withImage = {
      read: async () => [{ types: ['image/png'], getType: async () => png }],
    }
    expect(await readClipboardImage(withImage as unknown as Clipboard, 50)).toBe(png)
  })

  it('is not defeated by a clipboard API the browser does not implement', async () => {
    expect(await readClipboardImage(undefined, 50)).toBeNull()
  })
})
