import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from './api'

const POLL_MS = 2000

// Document library from GET /api/documents. While any document is still being ingested the list is polled
// (the same payload carries stage + chunk progress), and polling stops by itself once everything settled.
export function useDocuments() {
  const [documents, setDocuments] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const docs = await apiFetch('/api/documents')
      setDocuments(docs)
      setLoaded(true)
      setError(null)
      return docs
    } catch (err) {
      setError(err.message)
      return null
    }
  }, [])

  // Initial load (same shape as refresh, but the state updates happen in the promise callbacks)
  useEffect(() => {
    let cancelled = false
    apiFetch('/api/documents')
      .then((docs) => { if (!cancelled) { setDocuments(docs); setLoaded(true); setError(null) } })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [])

  const hasProcessing = documents.some((d) => d.status === 'processing')
  useEffect(() => {
    if (!hasProcessing) return undefined
    const timer = setInterval(refresh, POLL_MS)
    return () => clearInterval(timer)
  }, [hasProcessing, refresh])

  return { documents, loaded, error, refresh }
}
