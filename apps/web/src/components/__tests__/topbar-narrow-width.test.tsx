import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// The real stylesheet, as text — see rowmenu-layout.test.tsx for why this
// is read from disk rather than imported.
const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

/** What the stylesheet declares for `property` on an element carrying just
 *  this class, from its unconditional (outside any `@media`) rule only —
 *  used where a narrower-width override exists on purpose and this needs
 *  the base rule specifically, not "whichever comes last in the file
 *  including inside a media block" (see mobile-layout.test.tsx for the
 *  variant that walks into media blocks, which isn't what's wanted here). */
function declaredStyleUnconditional(className: string, property: string): string {
  const style = document.createElement('style')
  style.textContent = styles
  document.head.append(style)

  const el = document.createElement('div')
  el.className = className
  document.body.append(el)

  let value = ''
  for (const rule of Array.from(style.sheet?.cssRules ?? [])) {
    if (!(rule instanceof CSSStyleRule)) continue
    const declared = rule.style.getPropertyValue(property)
    if (declared && el.matches(rule.selectorText)) value = declared
  }

  style.remove()
  el.remove()
  return value
}

/**
 * What the stylesheet declares for `property` on an element matching a
 * descendant selector like `.topbar h1` — `matches()` only evaluates a
 * combinator correctly against a real ancestor, so unlike the helper above
 * this builds the actual parent/child relationship rather than a single
 * bare element.
 */
function declaredStyleOnDescendant(
  ancestorClass: string,
  childTagAndClass: string,
  property: string,
): string {
  const style = document.createElement('style')
  style.textContent = styles
  document.head.append(style)

  const parent = document.createElement('div')
  parent.className = ancestorClass
  const [tag, ...classes] = childTagAndClass.split('.')
  const child = document.createElement(tag || 'span')
  child.className = classes.join(' ')
  parent.append(child)
  document.body.append(parent)

  let value = ''
  const walk = (rules: CSSRuleList) => {
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSStyleRule) {
        const declared = rule.style.getPropertyValue(property)
        if (declared && child.matches(rule.selectorText)) value = declared
      } else if (rule instanceof CSSMediaRule) {
        walk(rule.cssRules)
      }
    }
  }
  walk(style.sheet?.cssRules ?? ([] as unknown as CSSRuleList))

  parent.remove()
  return value
}

/**
 * The media condition guarding the `@media` block containing a `.topbar-actions
 * { display: none }` rule — there are two `.topbar-actions {}` blocks in the
 * file (the base rule and this narrow-width override), so this finds the one
 * that actually sets `display` to something other than the base `flex`.
 */
function topbarActionsHiddenAt(): string | null {
  const style = document.createElement('style')
  style.textContent = styles
  document.head.append(style)

  let condition: string | null = null
  const walk = (rules: CSSRuleList, media: string | null) => {
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSStyleRule && rule.selectorText === '.topbar-actions') {
        if (rule.style.getPropertyValue('display') === 'none') condition = media
      } else if (rule instanceof CSSMediaRule) {
        walk(rule.cssRules, rule.conditionText)
      }
    }
  }
  walk(style.sheet?.cssRules ?? ([] as unknown as CSSRuleList), null)

  style.remove()
  return condition
}

/**
 * A topbar with a real project name, environment, activity chip and branch
 * tag genuinely needs more room than a phone-width breakpoint (720px)
 * allows for beside three view tabs and four action buttons — below that
 * width but above true mobile, .topbar-actions used to be an unstyled
 * block whose buttons wrapped internally, two or three deep, overflowing
 * the topbar's fixed height and landing on top of the tabs above them.
 * Reported with a screenshot at a moderately narrow (not phone-width)
 * window.
 */
describe('the topbar degrades gracefully below full desktop width, before true mobile', () => {
  it('.topbar-actions never wraps its own buttons internally, by default', () => {
    expect(declaredStyleUnconditional('topbar-actions', 'display')).toBe('flex')
    expect(declaredStyleUnconditional('topbar-actions', 'flex-shrink')).toBe('0')
  })

  it('.view-toggle never clips a tab instead of letting the title shrink first', () => {
    expect(declaredStyleUnconditional('view-toggle', 'flex-shrink')).toBe('0')
  })

  it('the title and its metadata can shrink with an ellipsis, unlike before', () => {
    expect(declaredStyleOnDescendant('topbar', 'h1', 'text-overflow')).toBe('ellipsis')
    expect(declaredStyleOnDescendant('topbar', 'h1', 'min-width')).toBe('0px')
    expect(declaredStyleOnDescendant('topbar', 'span.sub', 'text-overflow')).toBe('ellipsis')
  })

  it('.topbar-actions hides in favour of the menu well before true mobile width, not at it', () => {
    const condition = topbarActionsHiddenAt()
    expect(condition).toBe('(max-width: 1100px)')
  })
})
