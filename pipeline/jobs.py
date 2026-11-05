"""
Background ingestion of one uploaded document, with its lifecycle tracked in `documents`:

    processing (stage: queued -> store -> parse -> embed -> extract -> finalize)  ->  ready | failed

Every stage is idempotent and cache-first (chunks exist? vectors present? graph cached/projected?), so a job
that was interrupted (e.g. a `uvicorn --reload` restart) is simply run again and resumes where it stopped.
Only one ingestion runs at a time: each one fans out into many embedding / LLM calls, so this caps the spend.
"""
import threading
import time
from typing import Callable, Optional

from database.database import get_postgres_connection
from pipeline import ingest
from pipeline.assemble_graph import run_pipeline as run_assemble_graph
from pipeline.sync_embeddings import synchronize_vectors

_INGEST_SLOT = threading.Semaphore(1)
_UPDATABLE = {"status", "stage", "chunks_total", "chunks_done", "error", "storage_path"}


def _update(conn, doc_id: str, **fields) -> None:
    """Persist lifecycle fields for the polling endpoint."""
    assert fields.keys() <= _UPDATABLE, f"unknown documents column(s): {set(fields) - _UPDATABLE}"
    assignments = ", ".join(f"{col} = %s" for col in fields)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE documents SET {assignments}, updated_at = now() WHERE id = %s;", (*fields.values(), doc_id))
    conn.commit()


def _scalar(conn, sql: str, params: tuple):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def _with_retry(fn, attempts: int = 2, delay: float = 3.0):
    """Both retried stages are idempotent. SystemExit (fatal auth errors in the extractor) is never retried."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts - 1:
                raise
            print(f"[JOB] transient failure ({e}); retrying in {delay:.0f}s")
            time.sleep(delay)


def run_ingest_job(doc_id: str, filename: str, data: Optional[bytes] = None,
                   on_complete: Optional[Callable[[str], None]] = None) -> None:
    """Run (or resume) the ingestion pipeline for an already-registered document. Never raises.

    data: the uploaded PDF bytes; None when resuming (the file is then re-read from Supabase Storage).
    on_complete(doc_id): called after the job finishes either way (e.g. to invalidate cached sync reports).
    """
    conn = get_postgres_connection()
    if conn is None:
        print(f"[JOB] {doc_id[:12]}: could not open a Postgres connection")
        return
    try:
        _update(conn, doc_id, status="processing", stage="queued", error=None)
        with _INGEST_SLOT:
            _run(conn, doc_id, filename, data)
    except (Exception, SystemExit) as e:  # SystemExit: the extractor exits on auth/permission errors
        print(f"[JOB] {doc_id[:12]}: FAILED: {e}")
        try:
            conn.rollback()
            _update(conn, doc_id, status="failed", stage=None, error=str(e)[:500] or type(e).__name__)
        except Exception as inner:
            print(f"[JOB] {doc_id[:12]}: could not record failure: {inner}")
    finally:
        try:
            conn.close()
        finally:
            if on_complete:
                try:
                    on_complete(doc_id)
                except Exception as e:
                    print(f"[JOB] on_complete failed: {e}")


def _run(conn, doc_id: str, filename: str, data: Optional[bytes]) -> None:
    # 1. Source file -> Storage (so the job can be resumed, and the PDF is cached)
    _update(conn, doc_id, stage="store")
    if data is not None:
        storage_path = ingest.upload_pdf_bytes(data, doc_id, filename)
        if storage_path:
            _update(conn, doc_id, storage_path=storage_path)

    # 2. Parse + chunk + insert chunk rows (skipped when the chunks already exist)
    _update(conn, doc_id, stage="parse")
    if not _scalar(conn, "SELECT count(*) FROM document_chunks WHERE document_id = %s;", (doc_id,)):
        if data is None:
            data = ingest.download_pdf_bytes(_scalar(conn, "SELECT storage_path FROM documents WHERE id = %s;", (doc_id,)))
        if data is None:
            raise ValueError("Source file is no longer available; please upload it again.")
        ingest.ingest_pdf_bytes(conn, doc_id, data)
    total = _scalar(conn, "SELECT count(*) FROM document_chunks WHERE document_id = %s;", (doc_id,))
    _update(conn, doc_id, chunks_total=total, chunks_done=0)

    # 3. Embeddings (only chunks that still lack a current vector)
    _update(conn, doc_id, stage="embed", chunks_done=0)
    _with_retry(lambda: synchronize_vectors(doc_id=doc_id, on_progress=lambda done, n: _update(conn, doc_id, chunks_done=done, chunks_total=n)))
    if _scalar(conn, "SELECT count(*) FROM document_chunks WHERE document_id = %s AND embedding IS NULL;", (doc_id,)):
        raise RuntimeError("Embedding did not complete for every chunk.")

    # 4. Graph extraction (cache-first) + Neo4j projection
    _update(conn, doc_id, stage="extract", chunks_done=0, chunks_total=total)
    result = _with_retry(lambda: run_assemble_graph(
        limit=None, doc_id=doc_id,
        on_progress=lambda done, n: _update(conn, doc_id, chunks_done=done, chunks_total=n)))

    # 5. Outcome: all chunks failed -> failed; some failed -> ready with a warning (vector search still works)
    _update(conn, doc_id, stage="finalize")
    failed = result["failed"]
    if result["total"] and failed >= result["total"]:
        raise RuntimeError("Graph extraction failed for every chunk.")
    warning = f"{failed} of {result['total']} chunks could not be graph-extracted; re-upload the file to retry them." if failed else None
    _update(conn, doc_id, status="ready", stage=None, chunks_done=total, chunks_total=total, error=warning)
    print(f"[JOB] {doc_id[:12]}: ready ({result})")


def list_processing_documents(conn) -> list[tuple[str, str]]:
    """(id, filename) of documents left in 'processing', e.g. by a server restart mid-ingestion."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, filename FROM documents WHERE status = 'processing' ORDER BY upload_timestamp LIMIT 50;")
        return cur.fetchall()


def spawn_ingest_job(doc_id: str, filename: str, data: Optional[bytes] = None,
                     on_complete: Optional[Callable[[str], None]] = None) -> threading.Thread:
    """Run a job on its own daemon thread (used for resumes at startup; uploads use FastAPI BackgroundTasks)."""
    thread = threading.Thread(target=run_ingest_job, args=(doc_id, filename, data, on_complete),
                              name=f"ingest-{doc_id[:8]}", daemon=True)
    thread.start()
    return thread
