import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Split from 'react-split'
import CausalGraph from './components/CausalGraph'
import MarkdownAnswer from './components/MarkdownAnswer'
import OntologyExplainer from './components/OntologyExplainer'
import AdvancedSettings from './components/AdvancedSettings'
import GraphSyncBadge from './components/GraphSyncBadge'
import DocumentPicker from './components/DocumentPicker'
import RunTimeline from './components/RunTimeline'
import { apiFetch } from './api'
import { clampParams, loadParams, saveParams } from './queryParams'
import {
  clearSession, isQueryPayload, loadDocId, loadPayload, loadQuery, loadRun,
  saveDocId, savePayload, saveQuery, saveRun
} from './sessionState'
import { formatRunTime, paramChips } from './runFormat'
import { useDocuments } from './useDocuments'
import { useRuns } from './useRuns'
import './App.css'

// Calibrated relevance (server-side, 0..1) → display band
const relevanceBand = (r = 0) => (r >= 0.6 ? 'strong' : r >= 0.3 ? 'partial' : 'weak')

const VISIBLE_CHUNKS = 3

function App() {
  const [query, setQuery] = useState(loadQuery)
  const [params, setParams] = useState(loadParams)
  const [payload, setPayload] = useState(loadPayload)
  const [docId, setDocId] = useState(loadDocId) // selected document; null = none (deselected)
  // The run shown on screen: { run_id, replay: false } for a live query, or { run_id, replay: true, ...frozen metadata }
  const [activeRun, setActiveRun] = useState(loadRun)
  const [isLoading, setIsLoading] = useState(false)
  const [isReplaying, setIsReplaying] = useState(false)
  const [error, setError] = useState(null)
  const [showAllChunks, setShowAllChunks] = useState(false)
  const [timelineOpen, setTimelineOpen] = useState(false)
  const replayToken = useRef(0) // lets a newer click / Reset discard a replay that is still loading

  const { documents, loaded: docsLoaded, error: docsError, refresh: refreshDocuments } = useDocuments()
  const {
    runs, hasMore, error: runsError, refresh: refreshRuns, loadMore: loadMoreRuns,
    deleteRun: deleteRunRecord, clearRuns: clearRunRecords
  } = useRuns()

  // Session cache (sessionStorage): survives a refresh, cleared when the tab closes
  useEffect(() => saveQuery(query), [query])
  useEffect(() => savePayload(payload), [payload])
  useEffect(() => saveDocId(docId), [docId])
  useEffect(() => saveRun(activeRun), [activeRun])

  // A stored id that no longer exists (document deleted, database reset) simply resolves to "no document"
  const activeDoc = useMemo(() => documents.find((d) => d.id === docId) ?? null, [documents, docId])
  const docReady = activeDoc?.status === 'ready'

  // A response always belongs to exactly one document, so switching documents clears it
  const selectDocument = (id) => {
    if (id === docId) return
    setDocId(id)
    setPayload(null)
    setActiveRun(null)
    setError(null)
    setShowAllChunks(false)
  }

  // After an upload: refresh the list first so the new document resolves, then select it
  const handleUploaded = async (id) => {
    await refreshDocuments()
    selectDocument(id)
  }

  const resetSession = () => {
    replayToken.current += 1 // cancel any replay still in flight
    setIsReplaying(false)
    setQuery('')
    setPayload(null)
    setDocId(null)
    setActiveRun(null)
    setError(null)
    setShowAllChunks(false)
    clearSession()
  }

  const updateParams = (next) => {
    const clamped = clampParams(next)
    setParams(clamped)
    saveParams(clamped)
  }

  const fetchQuery = async (e) => {
    e.preventDefault()
    if (!query.trim() || !activeDoc || !docReady) return

    replayToken.current += 1
    setIsReplaying(false)
    setIsLoading(true)
    setError(null)

    try {
      const data = await apiFetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: query.trim(), document_id: activeDoc.id, ...clampParams(params) })
      })
      setPayload(data)
      setActiveRun(data.run_id ? { run_id: data.run_id, replay: false } : null)
      setShowAllChunks(false)
      refreshRuns()
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  // Rehydrate a past run from the ledger: one DB read, no retrieval and no LLM call
  const openRun = async (summary) => {
    const token = ++replayToken.current
    setIsReplaying(true)
    setError(null)
    try {
      const run = await apiFetch(`/api/runs/${summary.run_id}`)
      if (token !== replayToken.current) return
      if (!isQueryPayload(run.response_payload)) throw new Error('This run was saved in an unsupported format.')
      setPayload(run.response_payload)
      setQuery(run.query)
      setDocId(run.document_id) // null when the document was deleted: the run stays viewable, nothing is selected
      setActiveRun({
        run_id: run.run_id,
        replay: true,
        created_at: run.created_at,
        parameters: run.parameters,
        document_name: run.document_name,
        document_deleted: run.document_id === null
      })
      setShowAllChunks(false)
      setTimelineOpen(false)
      refreshDocuments()
    } catch (err) {
      if (token === replayToken.current) setError(err.message)
    } finally {
      if (token === replayToken.current) setIsReplaying(false)
    }
  }

  const closeReplay = () => {
    setPayload(null)
    setActiveRun(null)
  }

  // A run that left the ledger is no longer "the run on screen": a replayed snapshot is closed, while a live
  // result simply stays visible without a ledger id
  const releaseActiveRun = () => {
    if (activeRun?.replay) closeReplay()
    else setActiveRun(null)
  }

  const deleteRun = async (run) => {
    if (await deleteRunRecord(run.run_id) && activeRun?.run_id === run.run_id) releaseActiveRun()
  }

  const clearHistory = async () => {
    if (!window.confirm(`Delete the entire run history (${runs.length}${hasMore ? '+' : ''} runs, all documents)? This cannot be undone.`)) return
    if (await clearRunRecords() && activeRun) releaseActiveRun()
  }
  const closeTimeline = useCallback(() => setTimelineOpen(false), [])

  // Explicit graph structure (provenance.graph) with fallback to the legacy edge list
  const graphEdges = payload?.provenance?.graph?.edges ?? payload?.provenance?.causal_graph ?? []
  // Relation types the question asks about (backend intent detection); memoised so the graph isn't re-keyed
  const focusRelations = useMemo(() => payload?.provenance?.focus_relations ?? [], [payload])
  // Entity ids of the retrieved graph: lets the answer renderer resolve unquoted multi-word names
  const graphEntities = useMemo(() => payload?.provenance?.graph?.nodes?.map((n) => n.id) ?? [], [payload])

  return (
    <Split
      className="bifurcated-layout"
      sizes={[50, 50]}        // Initial 50/50 split
      minSize={[450, 400]}    // Min width in px for [Left Pane, Right Pane]
      gutterSize={6}          // Width of the draggable divider
      direction="horizontal"
      cursor="col-resize"
    >
      {/* S_continuous: Generative Text Space */}
      <div className="generative-pane">
        <header className="pane-header">
          <div className="title-group">
            <h2>Graph-RAG Knowledge Dashboard</h2>
            <OntologyExplainer />
          </div>
          <GraphSyncBadge refreshKey={payload} repaired={payload?.telemetry?.graph_sync?.repaired ?? 0} />
        </header>

        {/* Document context: select / deselect the active PDF, upload new ones, open the run history */}
        <div className="doc-bar">
          <DocumentPicker
            documents={documents}
            loaded={docsLoaded}
            error={docsError}
            selectedId={docId}
            activeDoc={activeDoc}
            onSelect={selectDocument}
            onUploaded={handleUploaded}
            disabled={isLoading}
          />
          <button type="button" className="history-btn" onClick={() => setTimelineOpen(true)} aria-haspopup="dialog" title="Run Timeline: past queries across all documents">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7v5l3 2" />
            </svg>
            History
            {runs.length > 0 && <span className="history-count">{runs.length}</span>}
          </button>
        </div>

        <RunTimeline
          open={timelineOpen}
          onClose={closeTimeline}
          runs={runs}
          error={runsError}
          hasMore={hasMore}
          onLoadMore={loadMoreRuns}
          onRefresh={refreshRuns}
          activeRunId={activeRun?.run_id ?? null}
          onSelect={openRun}
          onDelete={deleteRun}
          onClearAll={clearHistory}
          disabled={isLoading}
        />

        <form onSubmit={fetchQuery} className="query-form">
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Initialize hybrid query... (e.g., What is the structural effect of continuous label mixing on macro F1 scores?)"
            rows={4}
          />

          {/* Query State Space: retrieval + generation hyperparameters */}
          <AdvancedSettings params={params} onChange={updateParams} disabled={isLoading} />

          <div className="controls-row">
            <span className="depth-hint">
              {params.k_hops === 1 && '1-Hop (Local)'}
              {params.k_hops === 2 && '2-Hop (Extended)'}
              {params.k_hops === 3 && '3-Hop (Global Topology)'}
              {' · '}{params.strict_grounding ? 'Strict grounding' : 'General knowledge allowed'}
            </span>

            <div className="controls-actions">
              <button
                type="button"
                className="reset-btn"
                onClick={resetSession}
                disabled={isLoading || (!query && !payload && !error && !docId)}
                title="Reset session: clears the prompt and response and deselects the document"
                aria-label="Reset session"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M3 12a9 9 0 1 0 3-6.7" />
                  <path d="M3 4v5h5" />
                </svg>
              </button>

              <button
                type="submit"
                disabled={isLoading || isReplaying || !docReady || query.trim().length < 2}
                title={docReady ? undefined : 'Select a ready document first'}
                className="submit-btn"
              >
                {isLoading ? (
                  <>
                    <svg className="spinner" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                      <circle className="spinner-track" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="spinner-head" d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
                    </svg>
                    Traversing (k={params.k_hops})...
                  </>
                ) : (
                  'Run Query'
                )}
              </button>
            </div>
          </div>
        </form>

        {error && <div className="error-boundary">Error: {error}</div>}

        {/* A replayed run is a frozen snapshot from the ledger, not a fresh computation */}
        {activeRun?.replay && payload && (
          <div className="replay-banner" role="status">
            <span className="replay-badge">Past run</span>
            <span className="replay-text">
              {formatRunTime(activeRun.created_at)} · {activeRun.document_name}
              {activeRun.document_deleted && ' (document deleted)'}
            </span>
            <span className="replay-chips">
              {paramChips(activeRun.parameters).map((c) => <span key={c} className="advanced-chip">{c}</span>)}
            </span>
            <span className="replay-note">Frozen result: no retrieval or LLM call.</span>
            <button type="button" className="replay-close" onClick={closeReplay}>Close</button>
          </div>
        )}

        {!payload && !error && !docId && !isReplaying && (
          <div className="start-card">
            <div className="start-icon" aria-hidden="true">📄</div>
            <p>Upload a PDF or pick a cached document above, then ask a question about it.</p>
            <p className="start-sub">Past queries from every document are under History.</p>
          </div>
        )}

        {/* Telemetry Dashboard Banner */}
        {payload?.telemetry && (
          <div className="telemetry-bar">
            <div className="telemetry-item">
              <span className="metric-icon">⏱️</span>
              <div>
                <div className="metric-label">Latency</div>
                <div className="metric-value">{(payload.telemetry.latency_ms / 1000).toFixed(2)}s</div>
              </div>
            </div>
            <div className="telemetry-item">
              <span className="metric-icon">🪙</span>
              <div>
                <div className="metric-label">Context Tokens</div>
                <div className="metric-value">{payload.telemetry.token_count.toLocaleString()}</div>
              </div>
            </div>
            <div className="telemetry-item">
              <span className="metric-icon">🔵</span>
              <div>
                <div className="metric-label">Nodes |V|</div>
                <div className="metric-value">{payload.telemetry.node_count}</div>
              </div>
            </div>
            <div className="telemetry-item">
              <span className="metric-icon">🔗</span>
              <div>
                <div className="metric-label">Edges |E|</div>
                <div className="metric-value">{payload.telemetry.edge_count}</div>
              </div>
            </div>
            <div className="telemetry-item">
              <span className="metric-icon">🔀</span>
              <div>
                <div className="metric-label">Depth Bound</div>
                <div className="metric-value">k = {payload.telemetry.k_hops}</div>
              </div>
            </div>
            {payload.telemetry.chunks_requested != null && (
              <div className="telemetry-item">
                <span className="metric-icon">📄</span>
                <div>
                  <div className="metric-label">Chunks</div>
                  <div className="metric-value">{payload.telemetry.chunks_returned}/{payload.telemetry.chunks_requested}</div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Synthesized Response */}
        {payload && (
          <div className="synthesis-output">
            <h3>Synthesized Response</h3>
            {payload.answer.trim().startsWith('INSUFFICIENT_CONTEXT') ? (
              <div className="llm-text answer-notice">{payload.answer}</div>
            ) : (
              <div className="llm-text markdown-body">
                <MarkdownAnswer text={payload.answer} entities={graphEntities} />
              </div>
            )}

            <h3>Semantic Anchors (Continuous State)</h3>
            <div className="chunks-container">
              {payload.provenance.source_chunks.length === 0 && (
                <div className="llm-text answer-notice">No chunks were retrieved from the vector database.</div>
              )}
              {payload.provenance.source_chunks
                .slice(0, showAllChunks ? undefined : VISIBLE_CHUNKS)
                .map((chunk, idx) => (
                <div key={idx} className="chunk-card">
                  <div className="chunk-header">
                    <span className="chunk-tag">Chunk {idx + 1}</span>
                    <div className="relevance-meta">
                      <span className="sim-raw" title="Raw cosine similarity">cos {chunk.similarity.toFixed(3)}</span>
                      <span className={`sim-score relevance-${relevanceBand(chunk.relevance)}`}>
                        Relevance {Math.round((chunk.relevance ?? 0) * 100)}%
                      </span>
                    </div>
                  </div>
                  <div className="relevance-bar">
                    <div
                      className={`relevance-fill relevance-${relevanceBand(chunk.relevance)}`}
                      style={{ width: `${Math.round((chunk.relevance ?? 0) * 100)}%` }}
                    />
                  </div>
                  <p>{chunk.text}</p>
                </div>
              ))}
              {payload.provenance.source_chunks.length > VISIBLE_CHUNKS && (
                <button type="button" className="show-more-btn" onClick={() => setShowAllChunks(!showAllChunks)}>
                  {showAllChunks
                    ? 'Show fewer chunks'
                    : `Show ${payload.provenance.source_chunks.length - VISIBLE_CHUNKS} more chunk${payload.provenance.source_chunks.length - VISIBLE_CHUNKS === 1 ? '' : 's'}`}
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* S_discrete: Topological Graph Space */}
      <div className="structural-pane">
        {payload && graphEdges.length > 0 ? (
          <CausalGraph key={activeRun?.run_id ?? 'live'} edges={graphEdges} focusRelations={focusRelations} />
        ) : payload ? (
          <div className="empty-state graph-empty">
            <div className="empty-icon">∅</div>
            <p>No graph relationships found for this query.</p>
            <p className="empty-sub">
              k = {payload.telemetry?.k_hops ?? params.k_hops} · {payload.telemetry?.chunks_returned ?? payload.provenance.source_chunks.length} chunk(s) retrieved.
              Try more semantic chunks or a deeper traversal.
            </p>
          </div>
        ) : (
          <div className="empty-state">
            <div className="empty-icon">🕸️</div>
            <p>{activeDoc ? 'G_local topological mapping will render here upon query execution.' : 'Select or upload a document to begin. Its local knowledge graph will render here.'}</p>
          </div>
        )}
      </div>
    </Split>
  )
}

export default App