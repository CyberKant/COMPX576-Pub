// Force Dynamics Ontology Configuration — shared by the graph legend, answer edge chips
// and the ontology explainer. Descriptions mirror the backend extraction prompt
// (ontologies/hybrid_semantic_partition.py, "ORTHOGONAL BASIS VECTORS").
export const FORCE_ONTOLOGY = {
  DYNAMIC: {
    title: 'Dynamic Forces (Change & Generation)',
    relations: {
      ACTUALIZES: { color: '#7410f7', math: '0 → 1', desc: 'Vector of generation. An active force brings a concept or entity into existence.' },
      MODULATES: { color: '#fa9e00', math: 'x → x′', desc: 'Vector of transformation. A continuous force alters the state, phase, or intensity of an entity without destroying it.' },
      POTENTIATES: { color: '#10b981', math: 'Affordance', desc: 'Vector of affordance. Removes a barrier or provides the necessary substrate/conditions to act.' }
    }
  },
  STRUCTURAL: {
    title: 'Structural Forces (Mereology & Limits)',
    relations: {
      COMPOSES: { color: '#3b82f6', math: '∑xᵢ → X', desc: 'Vector of synthesis. Smaller active differences are bound together to form a larger unified whole.' },
      CONSTRAINS: { color: '#ef4444', math: 'Lim(x)', desc: 'Vector of restriction. Applies pressure to limit, bound, or halt the natural trajectory of another.' },
      DIFFERENTIATES: { color: '#db9cf8', math: 'x ⟂ y', desc: 'Vector of divergence / contrast. Two entities are set apart because they differ in design, data, parameters, role or outcome, or one is a variant of the other.' }
    }
  },
  ASSOCIATIVE: {
    title: 'Associative Mappings',
    relations: {
      MAPS_TO: { color: '#a8a29e', math: 'x ≅ y', desc: 'Vector of correlation. Spatial, structural, or conceptual alignment where no direct causal energy is transferred.' },
      DEFAULT: { color: '#64748b', math: 'Relation', desc: 'Undefined systemic connection.' }
    }
  }
}

export const RELATION_COLORS = Object.values(FORCE_ONTOLOGY).reduce((acc, category) => {
  Object.entries(category.relations).forEach(([key, val]) => { acc[key] = val.color })
  return acc
}, {})

// The kind of question that asks for each relation type. The backend maps a question's wording onto these
// (keyword rules; one tiny cached LLM call when the rules find nothing) and foregrounds the matching edges.
export const QUESTION_HINTS = {
  ACTUALIZES: 'What does X produce or create?',
  MODULATES: 'How does X affect or tune Y?',
  POTENTIATES: 'What does X require or depend on?',
  COMPOSES: 'What is X made of, or part of?',
  CONSTRAINS: 'What limits or blocks X?',
  DIFFERENTIATES: 'How does X differ from Y? What separates them?',
  MAPS_TO: 'What does X correspond to or measure?',
  DEFAULT: ''
}

// How each basis vector reads in Talmy's Agonist/Antagonist schema (ontology explainer).
// COMPOSES and MAPS_TO are this project's extensions beyond Talmy's core force schema.
export const TALMY_READING = {
  ACTUALIZES: 'Onset causation — an Antagonist drives the Agonist from rest into existence.',
  MODULATES: 'Sustained causation — an ongoing force shifts the Agonist’s state without ending it.',
  POTENTIATES: 'Letting — a blocking Antagonist disengages, so the Agonist’s own tendency can act.',
  COMPOSES: 'Extension (mereology) — parts are bound into a whole; force as cohesion.',
  CONSTRAINS: 'Hindering — a stronger Antagonist blocks or limits the Agonist’s tendency.',
  DIFFERENTIATES: 'Opposition — Agonist and Antagonist push against each other in tension; in text, two entities set apart by what one has or does that the other does not.',
  MAPS_TO: 'Extension (association) — alignment with no force transferred.',
  DEFAULT: 'Fallback when no basis vector fits.'
}
