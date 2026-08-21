/**
 * Copy text, including on an instance without HTTPS.
 *
 * `navigator.clipboard` exists only in a secure context. An instance reached by
 * IP address on plain HTTP — which is every instance before somebody points a
 * domain at it — does not have one, so the object is `undefined` and calling
 * `writeText` on it throws. Every copy button in the app did exactly that, and
 * threw into a `void`, so the button did nothing and said nothing. The one that
 * matters most is the harness sign-in URL, which is far too long to retype.
 *
 * So: the modern API when it is there, and the old `execCommand` path when it is
 * not. That one still works on plain HTTP in every browser that matters, and is
 * the only thing that does.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission refused, or a browser that offers the API and declines to
      // use it. Fall through rather than give up.
    }
  }

  try {
    const area = document.createElement('textarea')
    area.value = text
    // Off-screen rather than hidden: `display: none` and `visibility: hidden`
    // cannot be selected, and selection is how this works at all.
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.top = '-1000px'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    area.setSelectionRange(0, text.length)
    const copied = document.execCommand('copy')
    document.body.removeChild(area)
    return copied
  } catch {
    return false
  }
}
