import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// The real stylesheet, as text, so the rules below are the shipped ones —
// see rowmenu-layout.test.tsx for why this is read from disk rather than
// imported.
const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

/**
 * What the stylesheet declares for `property` on an element carrying just
 * this class, walking into `@media` blocks as well as top-level rules —
 * `.topbar`'s safe-area padding only exists inside one. Not a simulation of
 * any particular viewport (jsdom does no layout, and its `matchMedia` isn't
 * real), just confirmation the declaration exists against the right
 * selector, last-in-source wins same as the real cascade would once the
 * media condition holds.
 */
function declaredStyleAnywhere(className: string, property: string): string {
  const style = document.createElement('style')
  style.textContent = styles
  document.head.append(style)

  const el = document.createElement('div')
  el.className = className
  document.body.append(el)

  let value = ''
  const walk = (rules: CSSRuleList) => {
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSStyleRule) {
        const declared = rule.style.getPropertyValue(property)
        if (declared && el.matches(rule.selectorText)) value = declared
      } else if (rule instanceof CSSMediaRule) {
        walk(rule.cssRules)
      }
    }
  }
  walk(style.sheet?.cssRules ?? ([] as unknown as CSSRuleList))

  style.remove()
  el.remove()
  return value
}

/**
 * The text of the block starting at the `{` right after each occurrence of
 * `startNeedle`, found by counting braces rather than a lazy regex — safe
 * against another rule inside the block containing its own `{...}`. There
 * can be more than one match (`.topbar {` appears more than once in the
 * file, in different media blocks), so every one is returned.
 */
function sourceBlocks(source: string, startNeedle: string): string[] {
  const blocks: string[] = []
  let from = 0
  for (;;) {
    const start = source.indexOf(startNeedle, from)
    if (start === -1) return blocks
    const open = start + startNeedle.length - 1
    let depth = 0
    for (let i = open; i < source.length; i++) {
      if (source[i] === '{') depth++
      else if (source[i] === '}') {
        depth--
        if (depth === 0) {
          blocks.push(source.slice(open + 1, i))
          from = i + 1
          break
        }
      }
    }
  }
}

/**
 * The mobile topbar overlapping iOS's status bar / notch (issue #150):
 * `.topbar` reserved no room for it at all, so the sidebar toggle and title
 * rendered straight underneath. Verified visually against real Chrome via
 * CDP with a genuine `env(safe-area-inset-top)` override — jsdom's own CSS
 * parser (cssstyle) cannot parse `max()`/`env()` and silently drops the
 * whole declaration, which is why this checks the source text directly
 * rather than the parsed rule the way `declaredStyleAnywhere` does above.
 */
describe('the mobile topbar clears the iOS status bar / notch', () => {
  it('reserves room via env(safe-area-inset-top), not a fixed guess', () => {
    // There are two `.topbar {` blocks in the file — the base rule and this
    // narrow-viewport override, distinguished by `flex-wrap: wrap`, which
    // only the latter declares.
    const topbarBlock = sourceBlocks(styles, '.topbar {').find((block) =>
      block.includes('flex-wrap: wrap'),
    )
    expect(topbarBlock).toContain('env(safe-area-inset-top)')
  })
})

/**
 * The home-screen usage strip running "Usage →" off the edge of a narrow
 * phone screen (issue #150): none of its flex children could shrink, so the
 * row overflowed the viewport instead of the least-important piece of text
 * truncating.
 */
describe('the usage strip truncates instead of overflowing on a narrow screen', () => {
  it('.strip-text can shrink below its content width', () => {
    // The flexbox trap: a flex item's default min-width is its own
    // unwrapped content width, so `overflow`/`text-overflow` alone do
    // nothing without this too.
    expect(parseFloat(declaredStyleAnywhere('strip-text', 'min-width'))).toBe(0)
    expect(declaredStyleAnywhere('strip-text', 'overflow')).toBe('hidden')
  })

  it('.usage-strip-more never gives up its own space', () => {
    // The only affordance saying the strip is tappable at all — it must stay
    // fully visible even while .strip-text is truncating beside it.
    expect(declaredStyleAnywhere('usage-strip-more', 'flex-shrink')).toBe('0')
  })
})
