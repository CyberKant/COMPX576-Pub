import { useEffect, useState } from 'react'
import { formatRunTime, paramChips } from '../runFormat'

// Run Timeline: a chronological feed of every past query across ALL documents (the query_runs ledger).
// Clicking a run replays its frozen response, so no retrieval or LLM call happens. Runs can be deleted one by
// one or all at once (the list updates as soon as the server confirms).
export default function RunTimeline({ open, onClose, runs, error, hasMore, onLoadMore, onRefresh, activeRunId, onSelect, onDelete, onClearAll, disabled }) {
  const [pending, setPending] = useState(null) // run id being deleted, or 'all'

  // Pull the newest runs whenever the drawer opens
  useEffect(() => {
    if (open) onRefresh()
  }, [open, onRefresh])

  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const remove = async (run) => {
    setPending(run.run_id)
    try { await onDelete(run) } finally { setPending(null) }
  }

  const clearAll = async () => {
    setPending('all')
    try { await onClearAll() } finally { setPending(null) }
  }

  return (
    <>
      <div className="timeline-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="timeline-drawer" role="dialog" aria-label="Run Timeline">
        <div className="timeline-header">
          <div>
            <h3>Run Timeline</h3>
            <p>Every past query, across all documents. Click one to replay it instantly (no LLM call).</p>
          </div>
          <button type="button" className="timeline-close" onClick={onClose} aria-label="Close run timeline">×</button>
        </div>

        {runs.length > 0 && (
          <div className="timeline-toolbar">
            <span>{runs.length}{hasMore ? '+' : ''} run{runs.length === 1 && !hasMore ? '' : 's'}</span>
            <button type="button" className="timeline-clear" onClick={clearAll} disabled={pending !== null}>
              {pending === 'all' ? 'Clearing…' : 'Clear all history'}
            </button>
          </div>
        )}

        {error && <div className="timeline-error" role="alert">Could not load runs: {error}</div>}

        <ol className="timeline-list">
          {runs.length === 0 && !error && <li className="timeline-empty">No runs yet. Run a query and it will appear here.</li>}
          {runs.map((run) => (
            <li key={run.run_id} className="run-row">
              <button
                type="button"
                className={`run-item${run.run_id === activeRunId ? ' is-active' : ''}`}
                onClick={() => onSelect(run)}
                disabled={disabled}
                aria-current={run.run_id === activeRunId ? 'true' : undefined}
              >
                <span className="run-doc">
                  <span className="run-doc-name" title={run.document_name}>{run.document_name}</span>
                  {run.document_id === null && <span className="run-deleted" title="The document was deleted; the run is kept as history">deleted</span>}
                </span>
                <span className="run-query">{run.query}</span>
                <span className="run-chips">
                  {paramChips(run.parameters).map((c) => <span key={c} className="advanced-chip">{c}</span>)}
                </span>
                <span className="run-meta">
                  {[formatRunTime(run.created_at),
                    run.latency_ms != null && `${(run.latency_ms / 1000).toFixed(1)}s`,
                    run.edge_count != null && `${run.edge_count} edges`].filter(Boolean).join(' · ')}
                </span>
              </button>
              <button
                type="button"
                className="run-delete"
                onClick={() => remove(run)}
                disabled={pending !== null}
                title="Delete this run from the history"
                aria-label={`Delete run: ${run.query}`}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M8 6V4h8v2" />
                  <path d="M19 6l-1 14H6L5 6" />
                  <path d="M10 11v6M14 11v6" />
                </svg>
              </button>
            </li>
          ))}
        </ol>

        {hasMore && (
          <button type="button" className="show-more-btn timeline-more" onClick={onLoadMore}>Load older runs</button>
        )}
      </aside>
    </>
  )
}
