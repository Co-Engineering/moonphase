import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Renders an agent's reply as the markdown it actually is, rather than a
 * wall of `*`, `#` and `` ` `` characters — this is model output, not user
 * input, so it is already well-formed markdown with real paragraph breaks.
 *
 * Raw HTML in the source is never rendered: react-markdown parses it as
 * literal text unless a plugin like rehype-raw is added, and none is here.
 * That is deliberate — the feed shows another party's output, verbatim.
 */
const COMPONENTS: Components = {
  a: ({ href, children, ...props }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
      {children}
    </a>
  ),
  code: ({ className, children, ...props }) => {
    // A fenced block gets a `language-xxx` class from remark; inline code
    // never does — the one reliable way to tell them apart since `inline`
    // was removed from react-markdown's own code props.
    if (/language-(\w+)/.exec(className ?? '')) {
      return (
        <code className={`md-code-block ${className ?? ''}`} {...props}>
          {children}
        </code>
      )
    }
    return (
      <code className="md-code-inline" {...props}>
        {children}
      </code>
    )
  },
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
