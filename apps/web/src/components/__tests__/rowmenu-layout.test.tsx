import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// The real stylesheet, as text, so the rules below are the shipped ones.
// Read from disk rather than imported: Vite's CSS plugin handles a `?raw`
// import of a stylesheet and hands back an empty string, which would make
// every assertion here pass against nothing.
const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

/**
 * Where the row menu ends up on screen.
 *
 * The menu is `position: absolute`, so it lands against the nearest positioned
 * ancestor. A project's menu was a sibling of its row rather than a child of
 * it, and `.tree-project` was never positioned — so every project's menu
 * resolved against the whole server block and stacked in its top-right corner,
 * on top of the server's own. Four menus, one clickable spot, and the last one
 * in the document winning every click.
 *
 * The component tests could not see it: jsdom does no layout, so a menu in the
 * wrong containing block renders exactly like one in the right place. What can
 * be checked without layout is the rule that decides the containing block —
 * every row that carries a menu has to establish one.
 */

/**
 * What the stylesheet declares for an element carrying just this class.
 *
 * jsdom does no layout and does not cascade a stylesheet into
 * `getComputedStyle`, so the rules are walked directly — but matched with the
 * browser's own selector engine via `matches()`, so this follows the real
 * selectors rather than looking for words in the file. Last declaration wins,
 * which for a single class with no competing rules is the cascade.
 */
function positionOf(className: string): string {
  const style = document.createElement('style')
  style.textContent = styles
  document.head.append(style)

  const el = document.createElement('div')
  el.className = className
  document.body.append(el)

  let value = 'static'
  for (const rule of Array.from(style.sheet?.cssRules ?? [])) {
    if (!(rule instanceof CSSStyleRule)) continue
    // A selector list is matched as a whole by matches(), which is what a
    // browser does too.
    if (rule.style.position && el.matches(rule.selectorText)) {
      value = rule.style.position
    }
  }

  style.remove()
  el.remove()
  return value
}

describe('a row menu is positioned against its own row', () => {
  // Every kind of row that carries one. A menu inside a row that establishes
  // no containing block escapes to an ancestor, which is the bug.
  it.each(['tree-server-row', 'tree-project-row', 'tree-session-row'])(
    '%s establishes a containing block',
    (className) => {
      expect(positionOf(className)).toBe('relative')
    },
  )

  it('the block a project sits in does not claim its menus', () => {
    // `.tree-server` wraps the server row and every project under it. While it
    // was the positioned ancestor, the projects' menus landed on it rather
    // than on their own rows — so it must not be the thing they resolve
    // against any more.
    expect(positionOf('tree-server')).not.toBe('relative')
  })
})
