import { useEffect, useState } from 'react'
import { API_BASE } from '../api'

// Neo4j is a projection of the Supabase extraction cache. The backend checks and repairs drift for free;
// this badge shows the state and offers the two explicit repairs:
//   Repair (free)  - re-project from the cache, 0 LLM tokens
//   Re-extract     - LLM extraction for ONLY the chunks whose cache is stale/missing (asks first)
export default function GraphSyncBadge({ refreshKey, repaired = 0 }) {
  const [info, setInfo] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // Re-check on mount and after every query (the backend answers from a 60 s TTL cache, ~5 ms)
  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/api/graph/sync`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => { if (!cancelled) { setInfo(data); setError(null) } })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [refreshKey])

  const heal = async (mode) => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/api/graph/sync`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setInfo(data.status)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!info && !error) return null

  const stale = info ? info.stale_cache_count + info.missing_cache_count : 0
  const drift = info ? info.drift_count + info.orphan_edges + info.orphan_entities : 0
  const level = busy ? 'busy' : error ? 'err' : drift > 0 ? 'warn' : stale > 0 ? 'stale' : 'ok'
  const label = busy
    ? 'Syncing…'
    : error ? 'Graph sync unavailable'
      : drift > 0 ? 'Graph drift detected'
        : stale > 0 ? `${stale} chunk${stale === 1 ? '' : 's'} need re-extraction`
          : repaired > 0 ? `Graph in sync · auto-repaired ${repaired}` : 'Graph in sync'
  const title = error
    ? error
    : `Neo4j projection vs Supabase cache · ${info.in_sync}/${info.chunks_total} chunks in sync · last check ${info.check_ms} ms · ontology ${info.current_ontology_hash}`

  return (
    <div className={`sync-badge sync-${level}`} title={title}>
      <span className="sync-dot" aria-hidden="true" />
      <span className="sync-label" role="status">{label}</span>
      {!busy && drift > 0 && (
        <button type="button" className="sync-btn" onClick={() => heal('project')}>Repair (free)</button>
      )}
      {!busy && stale > 0 && (
        <button
          type="button"
          className="sync-btn"
          onClick={() => {
            if (window.confirm(`Re-extract ${stale} chunk(s) with the LLM (about ${Math.ceil(stale / 4)} call${Math.ceil(stale / 4) === 1 ? '' : 's'})? Neo4j is then rebuilt from the cache.`)) heal('extract')
          }}
        >
          Re-extract
        </button>
      )}
    </div>
  )
}
