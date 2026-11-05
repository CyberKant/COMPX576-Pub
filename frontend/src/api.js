// Backend base URL (CORS on the backend allows exactly the Vite dev origin; see server_api/main.py)
export const API_BASE = 'http://localhost:8000'

// Mirrors MAX_UPLOAD_MB in server_api/main.py (the server enforces it; this only avoids a pointless upload)
export const MAX_UPLOAD_MB = 25

// FastAPI 422 responses carry a list of validation errors; flatten them into readable text
export const formatErrorDetail = (detail, status) => {
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((l) => l !== 'body').join('.') || 'request'}: ${d.msg}`).join('; ')
  }
  if (typeof detail === 'string') return detail
  return `HTTP Error: ${status}`
}

// fetch + JSON with the backend's error shape turned into a thrown Error
export async function apiFetch(path, options) {
  const response = await fetch(`${API_BASE}${path}`, options)
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new Error(formatErrorDetail(errorData.detail, response.status))
  }
  return response.json()
}
