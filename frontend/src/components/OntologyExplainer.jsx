import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import MarkdownAnswer from './MarkdownAnswer'
import { FORCE_ONTOLOGY, TALMY_READING, QUESTION_HINTS } from '../ontology'

const TITLE = 'How does this fixed RAG ontology work?'

const INTRO = `
### What makes this ontology “fixed”?

Most graph-RAG systems let the ontology drift with the corpus. Dynamic-ontology systems such as **TrustGraph** either let the LLM invent open-vocabulary relation labels per document, or bind extraction to a schema defined for each domain (a “poetry schema” vs an “academic schema”). Either way, the *relation vocabulary changes from one corpus to the next*.

This dashboard is a **proof of concept for the opposite approach**: entities are extracted freely from the text, but every relationship must be expressed in one **fixed, domain-invariant basis of 8 relation types** — the same ontology serves a fluid-dynamics textbook and a cooking recipe.

### Grounding: Talmy’s Force Dynamics

The basis comes from Leonard Talmy’s *Force Dynamics* (cognitive linguistics; “Force Dynamics in Language and Cognition”, *Cognitive Science*, 1988). Talmy showed that language encodes causation as an interaction of forces: an **Agonist** with an intrinsic tendency toward action or rest, opposed by an **Antagonist**, where the stronger force determines the **resultant**. *Causing*, *letting*, *helping* and *hindering* all fall out of this single schema.

### Why treat it as objective structure?

The working hypothesis of this project is that relationships between concepts in an LLM’s vector space behave like **physical forces**, not arbitrary labels. Force-dynamic primitives are shared across languages and domains, so they offer an *objective semantic grounding*: typing every edge as generation, transformation, enablement, restriction, opposition, composition, or force-free mapping turns the extracted graph into a structure whose paths can be followed like chains of physical cause and effect.
`

const OUTRO = `
### How retrieval uses it

1. The query is embedded and the closest chunks are found by vector search (Supabase / pgvector).
2. A $k$-hop breadth-first search walks the **typed** edges around those chunks in Neo4j.
3. The question's wording is mapped onto the relation type it asks about (“what separates…” → DIFFERENTIATES, “what limits…” → CONSTRAINS) and edges of that type next to the retrieved entities are pulled in and listed first. Keyword rules do this for free; only when they find nothing does one tiny, cached LLM call classify the question.
4. The LLM answers *only* from that hybrid context and cites the edges it relies on, e.g. (MANIFOLD_MIXING) -[MODULATES]-> (MODEL_C).

The graph is kept honest by a **cache-first sync**: the extraction cache in Supabase is the source of truth and Neo4j is a projection of it. Drift is detected and repaired for free; the LLM is only used again when the ontology prompt itself changes.

### Status

This is a proof of concept. The claims above are hypotheses under benchmark: semantic noise as the neighbourhood grows like $\\mathcal{O}(d^k)$ for $k \\ge 2$, and token cost against schema / validation-loop pipelines such as TrustGraph.
`

export default function OntologyExplainer() {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef(null)
  const panelRef = useRef(null)
  const closeRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const trigger = triggerRef.current
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()

    const onKey = (e) => {
      if (e.key === 'Escape') { setOpen(false); return }
      if (e.key !== 'Tab' || !panelRef.current) return
      // Keep keyboard focus inside the dialog
      const focusables = panelRef.current.querySelectorAll('button, [href], [tabindex]:not([tabindex="-1"])')
      if (!focusables.length) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
      trigger?.focus()
    }
  }, [open])

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="info-btn"
        onClick={() => setOpen(true)}
        aria-label={TITLE}
        title={TITLE}
      >
        i
      </button>

      {open && createPortal(
        <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false) }}>
          <div ref={panelRef} className="modal-panel" role="dialog" aria-modal="true" aria-labelledby="ontology-explainer-title">
            <div className="modal-header">
              <h3 id="ontology-explainer-title">{TITLE}</h3>
              <button ref={closeRef} type="button" className="modal-close" onClick={() => setOpen(false)} aria-label="Close">×</button>
            </div>

            <div className="modal-body markdown-body">
              <MarkdownAnswer text={INTRO} />

              <h3>The 8 basis vectors</h3>
              {Object.entries(FORCE_ONTOLOGY).map(([catKey, category]) => (
                <div key={catKey} className="basis-group">
                  <div className="basis-group-title">{category.title}</div>
                  {Object.entries(category.relations).map(([relKey, data]) => (
                    <div key={relKey} className="basis-row">
                      <div className="basis-bar" style={{ backgroundColor: data.color }} />
                      <div className="basis-body">
                        <div className="basis-head">
                          <span className="basis-name" style={{ color: data.color }}>{relKey}</span>
                          <span className="basis-math">{data.math}</span>
                        </div>
                        <div className="basis-desc">{data.desc}</div>
                        <div className="basis-talmy">{TALMY_READING[relKey]}</div>
                        {QUESTION_HINTS[relKey] && <div className="basis-ask">Ask: {QUESTION_HINTS[relKey]}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              ))}

              <MarkdownAnswer text={OUTRO} />
            </div>
          </div>
        </div>,
        document.body
      )}
    </>
  )
}
