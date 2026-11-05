import { useCallback, useState } from 'react'
import { apiFetch } from './api'

const PAGE = 50

// Run ledger feed from GET /api/runs (newest first, all documents, summaries only).
// refresh() pulls the newest page and merges it in front; loadMore() pages backwards with `before`.
export function useRuns() {
  const [runs, setRuns] = useState([])
  const [hasMore, setHasMore] = useState(true)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const page = await apiFetch(`/api/runs?limit=${PAGE}`)
      setRuns((prev) => {
        const fresh = new Set(page.map((r) => r.run_id))
        return [...page, ...prev.filter((r) => !fresh.has(r.run_id))]
      })
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const loadMore = useCallback(async () => {
    const oldest = runs[runs.length - 1]
    if (!oldest) return
    try {
      const page = await apiFetch(`/api/runs?limit=${PAGE}&before=${encodeURIComponent(oldest.created_at)}`)
      setRuns((prev) => [...prev, ...page.filter((r) => !prev.some((p) => p.run_id === r.run_id))])
      setHasMore(page.length === PAGE)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [runs])

  // Both removals update the list as soon as the server confirms, so the UI changes without a refresh
  const deleteRun = useCallback(async (runId) => {
    try {
      await apiFetch(`/api/runs/${runId}`, { method: 'DELETE' })
      setRuns((prev) => prev.filter((r) => r.run_id !== runId))
      setError(null)
      return true
    } catch (err) {
      setError(err.message)
      return false
    }
  }, [])

  const clearRuns = useCallback(async () => {
    try {
      await apiFetch('/api/runs', { method: 'DELETE' })
      setRuns([])
      setHasMore(false)
      setError(null)
      return true
    } catch (err) {
      setError(err.message)
      return false
    }
  }, [])

  return { runs, hasMore: hasMore && runs.length >= PAGE, error, refresh, loadMore, deleteRun, clearRuns }
}
