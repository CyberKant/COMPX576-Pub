// Per-tab session cache: the selected document, prompt and last response survive a page refresh but not a tab
// close. Kept separate from queryParams.js (settings live in localStorage and are not touched by Reset).
const QUERY_KEY = 'kdg.session.query.v1'
const PAYLOAD_KEY = 'kdg.session.payload.v1'
const DOC_KEY = 'kdg.session.doc.v1'
const RUN_KEY = 'kdg.session.run.v1'

const write = (key, value) => {
  try {
    if (value) sessionStorage.setItem(key, value)
    else sessionStorage.removeItem(key)
  } catch { /* storage unavailable or quota exceeded: persistence is best-effort */ }
}

const read = (key) => {
  try { return sessionStorage.getItem(key) } catch { return null }
}

// App.jsx dereferences payload.answer / payload.provenance.source_chunks unguarded, so anything that doesn't
// look like a QueryResponse is discarded. document_id distinguishes it from payloads of the single-document era.
export const isQueryPayload = (data) =>
  typeof data?.answer === 'string' &&
  Array.isArray(data?.provenance?.source_chunks) &&
  typeof data?.document_id === 'string'

export const loadQuery = () => read(QUERY_KEY) ?? ''

export function loadPayload() {
  try {
    const raw = read(PAYLOAD_KEY)
    if (!raw) return null
    const data = JSON.parse(raw)
    if (isQueryPayload(data)) return data
    sessionStorage.removeItem(PAYLOAD_KEY)
  } catch { /* corrupt JSON or storage unavailable */ }
  return null
}

export const loadDocId = () => read(DOC_KEY) || null

// activeRun = { run_id, replay, ...frozen run metadata when replay is true }
export function loadRun() {
  try {
    const data = JSON.parse(read(RUN_KEY))
    if (typeof data?.run_id === 'string') return data
  } catch { /* missing or corrupt */ }
  return null
}

export const saveQuery = (query) => write(QUERY_KEY, query)
export const savePayload = (payload) => write(PAYLOAD_KEY, payload ? JSON.stringify(payload) : '')
export const saveDocId = (docId) => write(DOC_KEY, docId)
export const saveRun = (run) => write(RUN_KEY, run ? JSON.stringify(run) : '')

export function clearSession() {
  write(QUERY_KEY, '')
  write(PAYLOAD_KEY, '')
  write(DOC_KEY, '')
  write(RUN_KEY, '')
}
