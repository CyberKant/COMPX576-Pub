// Query hyperparameter specs. Bounds mirror QueryRequest (server_api/main.py) and
// the *_RANGE constants in retrieval/query_engine.py.
export const PARAM_SPECS = {
  k_hops: {
    label: 'k-Hops', min: 1, max: 3, step: 1, default: 1,
    format: (v) => `k = ${v}`,
    tooltip: 'Graph depth. 1 = Direct neighbors. 2 = Neighbors of neighbors. Higher values reveal latent relationships but increase graph noise.'
  },
  top_k_chunks: {
    label: 'Semantic Chunks', min: 1, max: 15, step: 1, default: 5,
    format: (v) => `${v}`,
    tooltip: "Max number of chunks retrieved from the vector database. More chunks provide wider context but may dilute the LLM's analytical focus. (Default: 5)"
  },
  temperature: {
    label: 'Temperature', min: 0, max: 1, step: 0.05, default: 0.2,
    format: (v) => v.toFixed(2),
    tooltip: 'Generation entropy (0.0 to 1.0). 0.0 is strictly deterministic.'
  },
  top_p: {
    label: 'Top-P', min: 0.1, max: 1, step: 0.05, default: 0.9,
    format: (v) => v.toFixed(2),
    tooltip: 'Nucleus sampling limit. 0.9 truncates the lowest 10% of improbable tokens to reduce hallucinations.'
  }
}

export const STRICT_GROUNDING_TOOLTIP = 'If enabled, forces the model to answer ONLY using the retrieved graph/vector data.'

export const DEFAULT_PARAMS = {
  ...Object.fromEntries(Object.entries(PARAM_SPECS).map(([k, s]) => [k, s.default])),
  strict_grounding: true
}

const STORAGE_KEY = 'kdg.settings.v1'

// Bound every value to its spec; anything non-numeric falls back to the default.
// Floats are snapped to their step so the request body never carries 0.30000000000000004.
export function clampParams(params = {}) {
  const out = {}
  for (const [key, spec] of Object.entries(PARAM_SPECS)) {
    const v = Number(params[key])
    if (!Number.isFinite(v)) { out[key] = spec.default; continue }
    const bounded = Math.min(spec.max, Math.max(spec.min, v))
    const snapped = Math.round(bounded / spec.step) * spec.step
    out[key] = spec.step >= 1 ? Math.round(snapped) : Number(Math.min(spec.max, Math.max(spec.min, snapped)).toFixed(2))
  }
  out.strict_grounding = typeof params.strict_grounding === 'boolean' ? params.strict_grounding : DEFAULT_PARAMS.strict_grounding
  return out
}

export function loadParams() {
  try {
    return clampParams({ ...DEFAULT_PARAMS, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') })
  } catch {
    return { ...DEFAULT_PARAMS }
  }
}

export function saveParams(params) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(clampParams(params))) } catch { /* storage unavailable */ }
}
