// Display helpers shared by the Run Timeline and the "past run" banner

export const formatRunTime = (iso) => {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// Frozen parameter state of a run -> short chips ("k=2", "5 chunks", "T 0.20", ...)
export const paramChips = (p = {}) => [
  p.k_hops != null && `k=${p.k_hops}`,
  p.top_k_chunks != null && `${p.top_k_chunks} chunk${p.top_k_chunks === 1 ? '' : 's'}`,
  typeof p.temperature === 'number' && `T ${p.temperature.toFixed(2)}`,
  typeof p.top_p === 'number' && `p ${p.top_p.toFixed(2)}`,
  typeof p.strict_grounding === 'boolean' && (p.strict_grounding ? 'strict' : 'open'),
].filter(Boolean)
