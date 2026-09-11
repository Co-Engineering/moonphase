import { describe, expect, it } from 'vitest'
import { detectLinks, joinTerminalRows, shortenLinkForDisplay, trimTrailingPunctuation } from '../Terminal'

describe('joinTerminalRows', () => {
  it('keeps unrelated rows separate', () => {
    expect(
      joinTerminalRows([
        { text: 'first line', wrapped: false },
        { text: 'second line', wrapped: false },
      ]),
    ).toEqual(['first line', 'second line'])
  })

  it('joins a soft-wrapped row onto the one above with no inserted space', () => {
    expect(
      joinTerminalRows([
        { text: 'see https://example.com/really-lo', wrapped: false },
        { text: 'ng-path', wrapped: true },
      ]),
    ).toEqual(['see https://example.com/really-long-path'])
  })

  it('chains three wrapped rows onto the same logical line', () => {
    expect(
      joinTerminalRows([
        { text: 'aaa', wrapped: false },
        { text: 'bbb', wrapped: true },
        { text: 'ccc', wrapped: true },
      ]),
    ).toEqual(['aaabbbccc'])
  })

  it('treats a wrapped row with nothing above it as its own line', () => {
    // Can't actually happen from a real buffer, but should not throw or drop
    // the row silently.
    expect(joinTerminalRows([{ text: 'orphan', wrapped: true }])).toEqual(['orphan'])
  })

  it('returns an empty array for no rows', () => {
    expect(joinTerminalRows([])).toEqual([])
  })
})

describe('trimTrailingPunctuation', () => {
  it('strips a trailing period picked up from the enclosing sentence', () => {
    expect(trimTrailingPunctuation('https://example.com/path.')).toBe('https://example.com/path')
  })

  it('strips trailing closing punctuation of several kinds at once', () => {
    expect(trimTrailingPunctuation('https://example.com").')).toBe('https://example.com')
  })

  it('leaves a URL with no trailing punctuation untouched', () => {
    expect(trimTrailingPunctuation('https://example.com/path')).toBe('https://example.com/path')
  })

  it('leaves internal punctuation alone', () => {
    expect(trimTrailingPunctuation('https://example.com/a.b.c')).toBe('https://example.com/a.b.c')
  })
})

describe('detectLinks', () => {
  it('finds a single link on its own line', () => {
    expect(detectLinks(['Sign in at https://example.com/auth to continue.'])).toEqual([
      'https://example.com/auth',
    ])
  })

  it('finds more than one link on the same line', () => {
    expect(detectLinks(['see https://a.example.com and https://b.example.com'])).toEqual([
      'https://a.example.com',
      'https://b.example.com',
    ])
  })

  it('matches http as well as https, case-insensitively', () => {
    expect(detectLinks(['HTTPS://Example.com/x'])).toEqual(['HTTPS://Example.com/x'])
    expect(detectLinks(['http://example.com/y'])).toEqual(['http://example.com/y'])
  })

  it('trims trailing sentence punctuation off a matched link', () => {
    expect(detectLinks(['docs are at https://example.com/guide.'])).toEqual([
      'https://example.com/guide',
    ])
  })

  it('drops an exact duplicate seen on a later line', () => {
    expect(
      detectLinks(['see https://example.com/x here', 'also https://example.com/x here']),
    ).toEqual(['https://example.com/x'])
  })

  it('does not match plain text with no scheme', () => {
    expect(detectLinks(['example.com is not a link here'])).toEqual([])
  })

  it(
    'extends a link that runs to the end of its line onto the next line, ' +
      'the way a TUI wraps its own layout without xterm ever seeing it as a soft wrap',
    () => {
      expect(
        detectLinks(['open https://example.com/deeply/nested', '/path/that/kept/going here']),
      ).toEqual(['https://example.com/deeply/nested/path/that/kept/going'])
    },
  )

  it('does not extend across a line that starts with whitespace — an indented next line reads as new content, not more of the link', () => {
    expect(detectLinks(['https://example.com/end', '  next thing entirely'])).toEqual([
      'https://example.com/end',
    ])
  })

  it('does not extend past a genuinely blank line', () => {
    expect(detectLinks(['https://example.com/end', ''])).toEqual(['https://example.com/end'])
  })

  it('caps how many continuation lines it will chase', () => {
    const lines = ['https://example.com/a', 'b', 'c', 'd', 'e', 'f']
    const [link] = detectLinks(lines)
    // 4 hops max: the starting line plus up to 4 more segments joined on.
    expect(link).toBe('https://example.com/abcde')
  })

  it('finds nothing in an empty buffer', () => {
    expect(detectLinks([])).toEqual([])
  })
})

describe('shortenLinkForDisplay', () => {
  it('returns a short URL unchanged', () => {
    expect(shortenLinkForDisplay('https://example.com')).toBe('https://example.com')
  })

  it('truncates the middle of a long URL, keeping the start and end', () => {
    const url = `https://example.com/${'a'.repeat(100)}/end-of-path`
    const short = shortenLinkForDisplay(url, 40)
    expect(short.length).toBeLessThanOrEqual(40)
    expect(short).toContain('…')
    expect(short.startsWith('https://example.com')).toBe(true)
    expect(short.endsWith('end-of-path')).toBe(true)
  })
})
