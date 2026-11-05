import { useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { RELATION_COLORS } from '../ontology'
import { prepareAnswer, parseEdgeToken } from '../citations'

// LLM output is untrusted: no rehype-raw (raw HTML stays escaped) and KaTeX keeps trust: false.
const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [[rehypeKatex, { throwOnError: false, strict: 'ignore' }]]

const prettify = (id) => String(id).replace(/_/g, ' ')

const withAlpha = (hex, alpha) => {
  const v = parseInt(hex.slice(1), 16)
  return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${alpha})`
}

function EdgeChip({ edge }) {
  const { src, rel, tgt } = edge
  const color = RELATION_COLORS[rel] || RELATION_COLORS.DEFAULT // unknown relations still render as a chip
  return (
    <span className="edge-chip" title={`(${src}) -[${rel}]-> (${tgt})`}>
      <span className="edge-node">{prettify(src)}</span>
      <span className="edge-arrow">→</span>
      <span className="edge-rel" style={{ color, borderColor: withAlpha(color, 0.6), background: withAlpha(color, 0.12) }}>
        {rel}
      </span>
      <span className="edge-arrow">→</span>
      <span className="edge-node">{prettify(tgt)}</span>
    </span>
  )
}

const COMPONENTS = {
  code({ className, children }) {
    const edge = !className ? parseEdgeToken(String(children)) : null
    if (edge) return <EdgeChip edge={edge} />
    return <code className={className}>{children}</code>
  },
  a({ href, children }) {
    return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
  }
}

// `entities`: ids of the retrieved graph nodes, used to resolve multi-word names that arrive unquoted
export default function MarkdownAnswer({ text, entities }) {
  const prepared = useMemo(() => prepareAnswer(text, entities), [text, entities])
  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={COMPONENTS}>
      {prepared}
    </ReactMarkdown>
  )
}
