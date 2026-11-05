import { useEffect, useRef, useState } from 'react'
import { apiFetch, MAX_UPLOAD_MB } from '../api'

const STAGE_LABEL = {
  queued: 'Queued',
  store: 'Saving PDF',
  parse: 'Reading PDF',
  embed: 'Embedding',
  extract: 'Extracting graph',
  finalize: 'Finalizing'
}

const formatSize = (bytes) => (bytes == null ? '' : bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`)

// Progress inside the current ingestion stage, or null when the stage has no countable work yet
const progressPct = (doc) => (doc.chunks_total ? Math.min(100, Math.round((100 * doc.chunks_done) / doc.chunks_total)) : null)

function StatusChip({ doc }) {
  if (doc.status === 'ready') {
    return <span className={`doc-chip doc-chip-ready${doc.error ? ' has-warning' : ''}`} title={doc.error || 'Ready for queries'}>{doc.error ? 'Ready ⚠' : 'Ready'}</span>
  }
  if (doc.status === 'failed') {
    return <span className="doc-chip doc-chip-failed" title={doc.error || 'Ingestion failed'}>Failed</span>
  }
  const pct = progressPct(doc)
  return (
    <span className="doc-chip doc-chip-processing">
      {STAGE_LABEL[doc.stage] ?? 'Processing'}{pct != null ? ` ${pct}%` : '…'}
    </span>
  )
}

// Document library: select / deselect the active document (a query is always scoped to exactly one) and upload
// new PDFs. Ingestion runs in the background; the parent polls the list and the chips show live progress.
export default function DocumentPicker({ documents, loaded, error, selectedId, activeDoc, onSelect, onUploaded, disabled }) {
  const [open, setOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [notice, setNotice] = useState(null) // { kind: 'ok' | 'err', text }
  const rootRef = useRef(null)
  const fileRef = useRef(null)

  // Close the list on outside click / Escape
  useEffect(() => {
    if (!open) return undefined
    const onPointer = (e) => { if (!rootRef.current?.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const choose = (id) => {
    onSelect(id)
    setOpen(false)
  }

  const handleFile = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow picking the same file again
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setNotice({ kind: 'err', text: 'Only .pdf files are supported.' })
      return
    }
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setNotice({ kind: 'err', text: `File is larger than the ${MAX_UPLOAD_MB} MB limit.` })
      return
    }

    setUploading(true)
    setNotice(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const result = await apiFetch('/api/upload', { method: 'POST', body: form })
      await onUploaded(result.document_id)
      setNotice({
        kind: 'ok',
        text: result.cached ? `"${result.filename}" is already cached and ready.` : `"${result.filename}" uploaded. Processing in the background…`
      })
      // A success note is transient (the status chip carries the lasting state); errors stay until the next upload
      setTimeout(() => setNotice((n) => (n?.kind === 'ok' ? null : n)), 6000)
    } catch (err) {
      setNotice({ kind: 'err', text: err.message })
    } finally {
      setUploading(false)
    }
  }

  const label = activeDoc
    ? activeDoc.filename
    : selectedId && !loaded ? 'Loading documents…' : 'No document selected'

  return (
    <div className="doc-picker" ref={rootRef}>
      <div className="doc-row">
        <div className="doc-select">
          <button
            type="button"
            className={`doc-trigger${open ? ' is-open' : ''}`}
            aria-haspopup="listbox"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            disabled={disabled}
          >
            <svg className="doc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
              <path d="M14 3v5h5" />
            </svg>
            <span className={`doc-label${activeDoc ? '' : ' is-empty'}`} title={activeDoc?.filename}>{label}</span>
            {activeDoc && <StatusChip doc={activeDoc} />}
            <span className="doc-caret" aria-hidden="true">{open ? '▴' : '▾'}</span>
          </button>

          {open && (
            <ul className="doc-list" role="listbox" aria-label="Documents">
              <li role="none">
                <button type="button" role="option" aria-selected={!selectedId} className={`doc-option${!selectedId ? ' is-active' : ''}`} onClick={() => choose(null)}>
                  <span className="doc-option-name">No document (deselect)</span>
                </button>
              </li>
              {documents.length === 0 && (
                <li className="doc-empty" role="none">{loaded ? 'No documents yet. Upload a PDF to begin.' : 'Loading…'}</li>
              )}
              {documents.map((doc) => (
                <li key={doc.id} role="none">
                  <button type="button" role="option" aria-selected={doc.id === selectedId} className={`doc-option${doc.id === selectedId ? ' is-active' : ''}`} onClick={() => choose(doc.id)}>
                    <span className="doc-option-top">
                      <span className="doc-option-name" title={doc.filename}>{doc.filename}</span>
                      <StatusChip doc={doc} />
                    </span>
                    <span className="doc-option-meta">
                      {[formatSize(doc.size_bytes), doc.upload_timestamp && new Date(doc.upload_timestamp).toLocaleDateString()].filter(Boolean).join(' · ')}
                    </span>
                    {doc.status === 'processing' && (
                      <span className="doc-progress" aria-hidden="true">
                        <span className="doc-progress-fill" style={{ width: `${progressPct(doc) ?? 6}%` }} />
                      </span>
                    )}
                    {doc.error && <span className={`doc-option-error${doc.status === 'ready' ? ' is-warning' : ''}`}>{doc.error}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <input ref={fileRef} type="file" accept="application/pdf,.pdf" className="doc-file-input" onChange={handleFile} tabIndex={-1} aria-hidden="true" />
        <button type="button" className="doc-upload-btn" onClick={() => fileRef.current?.click()} disabled={uploading || disabled} title={`Upload a PDF (max ${MAX_UPLOAD_MB} MB)`}>
          {uploading ? 'Uploading…' : '+ Upload PDF'}
        </button>
      </div>

      {activeDoc?.status === 'processing' && (
        <div className="doc-note" role="status">Ingesting this document. Queries unlock when it is ready.</div>
      )}
      {activeDoc?.status === 'failed' && (
        <div className="doc-note doc-note-err" role="alert">Ingestion failed{activeDoc.error ? `: ${activeDoc.error}` : '.'} Upload the file again to retry.</div>
      )}
      {!activeDoc && !selectedId && (
        <div className="doc-note">Select a document or upload a PDF to start querying.</div>
      )}
      {notice && <div className={`doc-note${notice.kind === 'err' ? ' doc-note-err' : ' doc-note-ok'}`} role={notice.kind === 'err' ? 'alert' : 'status'}>{notice.text}</div>}
      {error && <div className="doc-note doc-note-err" role="alert">Could not load documents: {error}</div>}
    </div>
  )
}
