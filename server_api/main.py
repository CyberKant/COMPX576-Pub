import asyncio
import datetime
import os
import subprocess
import sys
import uuid
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager

# 1. Path & Venv Resolution: Anchor execution to the project root and auto-delegate to .venv (or similar) if invoked via bare `python`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if __name__ == "__main__" and _VENV_PY.exists() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    raise SystemExit(subprocess.call([str(_VENV_PY), *sys.argv]))

sys.path.append(str(PROJECT_ROOT))

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from psycopg2.extras import Json
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated, Any, List, Literal, Optional

# Import the engine
from retrieval.query_engine import HybridQueryEngine
from database.database import get_postgres_connection
from pipeline import graph_sync, ingest, jobs

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# ---------------------------------------------------------------------------
# 2. State Space Serialization Schemas (Pydantic)
# ---------------------------------------------------------------------------
# These strictly enforce the topology of P_out and dynamic request variables

class Edge(BaseModel):
    source: Optional[str] = "UNKNOWN"
    relation: str
    detail: Optional[str] = ""
    target: Optional[str] = "UNKNOWN"

class Chunk(BaseModel):
    chunk_id: str
    text: str
    similarity: float
    relevance: float  # calibrated [0, 1] from raw cosine (see query_engine.SIM_FLOOR/SIM_CEIL)

class GraphNode(BaseModel):
    id: str
    degree: int

class Graph(BaseModel):
    # Explicit graph structure; {nodes: [], edges: []} when the traversal yields nothing
    nodes: List[GraphNode] = []
    edges: List[Edge] = []

class Provenance(BaseModel):
    source_chunks: List[Chunk]
    causal_graph: List[Edge]  # kept for backward compatibility (same as graph.edges)
    graph: Graph
    focus_relations: List[str] = []  # relation types the question asks about (intent detection)

class GraphSyncInfo(BaseModel):
    # Neo4j (projection) vs Supabase extraction cache, checked before the traversal
    status: str  # in_sync | drift | stale_cache | unknown
    repaired: int = 0  # chunks/edges repaired for free during this request
    stale_cache: int = 0  # chunks whose cache needs LLM re-extraction
    check_ms: float = 0
    error: Optional[str] = None

class Telemetry(BaseModel):
    latency_ms: float
    token_count: int
    node_count: int
    edge_count: int
    k_hops: int
    # Effective (post-clamp) hyperparameters, echoed for benchmarking
    chunks_requested: int
    chunks_returned: int
    temperature: float
    top_p: float
    strict_grounding: bool
    graph_sync: Optional[GraphSyncInfo] = None
    focus_relations: List[str] = []
    intent_source: str = "none"  # rules | cache | llm | none

class QueryRequest(BaseModel):
    # The document every retrieval step (vector search + graph traversal) is strictly bounded to
    document_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] = Field(
        ..., description="Id (SHA-256) of a ready document, from GET /api/documents")
    # Bounds mirror retrieval/query_engine.py (*_RANGE) and frontend/src/queryParams.js
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=1000)] = Field(
        ..., description="Stripped of leading/trailing whitespace")
    k_hops: int = Field(default=1, ge=1, le=3, description="Graph traversal depth")
    top_k_chunks: int = Field(default=5, ge=1, le=15, description="Max chunks from vector search")
    temperature: float = Field(default=0.2, ge=0.0, le=1.0, description="Generation entropy")
    top_p: float = Field(default=0.9, ge=0.1, le=1.0, description="Nucleus sampling limit")
    strict_grounding: bool = Field(default=True, description="Answer only from retrieved context")

class QueryResponse(BaseModel):
    query: str
    answer: str
    provenance: Provenance
    telemetry: Telemetry # Extends payload with benchmark data
    document_id: Optional[str] = None
    run_id: Optional[str] = None  # ledger id (query_runs); None if the run could not be logged

class DocumentInfo(BaseModel):
    id: str
    filename: str
    status: Literal["processing", "ready", "failed"]
    stage: Optional[str] = None  # queued | store | parse | embed | extract | finalize while processing
    chunks_total: Optional[int] = None
    chunks_done: int = 0
    error: Optional[str] = None  # failure reason, or a warning on a ready document
    size_bytes: Optional[int] = None
    upload_timestamp: Optional[datetime.datetime] = None

class UploadResponse(BaseModel):
    document_id: str
    filename: str
    status: Literal["processing", "ready", "failed"]
    cached: bool  # True when these exact bytes were already ingested (no work was queued)

class RunSummary(BaseModel):
    run_id: str
    document_id: Optional[str] = None  # None once the document was deleted
    document_name: str
    query: str
    parameters: dict
    created_at: datetime.datetime
    latency_ms: Optional[float] = None
    edge_count: Optional[int] = None

class RunDetail(RunSummary):
    response_payload: dict[str, Any]  # frozen QueryResponse, replayed by the UI with no retrieval or LLM work

# ---------------------------------------------------------------------------
# 3. Singleton Instantiation & Application Lifecycle
# ---------------------------------------------------------------------------
# We hold the engine in global memory to prevent DB reconnection overhead on every request.
engine_instance = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup boundary: Initialize database pools and LLM clients once
    print("[SYSTEM] Initializing HybridQueryEngine...")
    engine = HybridQueryEngine()
    engine_instance["engine"] = engine
    # Verify the Neo4j projection against the Supabase cache; drift is repaired for free (no LLM tokens).
    # Stale caches (ontology prompt changed) are only re-extracted automatically if AUTO_HEAL_EXTRACT=1.
    try:
        print(f"[SYSTEM] Graph sync: {engine.sync.startup(engine.pg_conn, engine.neo4j_driver)}")
    except Exception as e:
        print(f"[WARN] Graph sync at startup failed (serving anyway): {e}")
    # Ingestions interrupted by a restart (e.g. a --reload) are resumed: every stage is idempotent
    try:
        conn = get_postgres_connection()
        if conn is not None:
            try:
                for doc_id, filename in jobs.list_processing_documents(conn):
                    print(f"[SYSTEM] Resuming interrupted ingestion: {filename} ({doc_id[:12]}...)")
                    jobs.spawn_ingest_job(doc_id, filename, None, on_complete=engine.sync.invalidate)
            finally:
                conn.close()
    except Exception as e:
        print(f"[WARN] Could not resume interrupted ingestions: {e}")
    print("[SYSTEM] Engine Online. Bipartite manifold ready.")
    yield
    # Shutdown boundary: Clean up resources if necessary
    engine_instance.clear()

app = FastAPI(title="Hybrid Graph-RAG Enterprise API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# 4. CORS Middleware (Frontend Bridge)
# ---------------------------------------------------------------------------
# Permits the asynchronous Javascript client (e.g., React on port 3000) to query this server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ # Isolate to the interface
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 5. Shared helpers
# ---------------------------------------------------------------------------
# Blocking database work runs via asyncio.to_thread on a per-request connection (never the engine's shared one),
# so a slow query or an ingestion in progress cannot stall the event loop or the polling endpoints.

def get_engine() -> HybridQueryEngine:
    engine: HybridQueryEngine = engine_instance.get("engine")
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    return engine

@contextmanager
def pg_connection():
    conn = get_postgres_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database unavailable.")
    try:
        yield conn
    finally:
        conn.close()

_DOC_COLUMNS = ("id", "filename", "status", "stage", "chunks_total", "chunks_done", "error", "size_bytes", "upload_timestamp")
_DOC_SELECT = f"SELECT {', '.join(_DOC_COLUMNS)} FROM documents"

def _doc_info(row) -> DocumentInfo:
    return DocumentInfo(**dict(zip(_DOC_COLUMNS, row)))

# ---------------------------------------------------------------------------
# 6. Documents: upload (async ingestion), list/poll, delete
# ---------------------------------------------------------------------------

def _register_upload(data: bytes, filename: str) -> tuple[UploadResponse, bool]:
    """Identity = SHA-256 of the bytes. Returns (response, whether a background job must be started)."""
    doc_id = ingest.compute_doc_id(data)
    with pg_connection() as conn:
        if ingest.register_document(conn, doc_id, filename, len(data)):
            return UploadResponse(document_id=doc_id, filename=filename, status="processing", cached=False), True
        with conn.cursor() as cur:
            # A failed document is retried (atomic claim, so two simultaneous re-uploads start only one job)
            cur.execute("UPDATE documents SET status = 'processing', stage = 'queued', error = NULL, updated_at = now() "
                        "WHERE id = %s AND status = 'failed' RETURNING filename;", (doc_id,))
            retried = cur.fetchone()
            conn.commit()
            if retried:
                return UploadResponse(document_id=doc_id, filename=retried[0], status="processing", cached=False), True
            cur.execute("SELECT filename, status FROM documents WHERE id = %s;", (doc_id,))
            name, status = cur.fetchone()
        return UploadResponse(document_id=doc_id, filename=name, status=status, cached=status == "ready"), False

@app.post("/api/upload", response_model=UploadResponse, status_code=202)
async def upload_document(response: Response, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """
    Accepts a PDF and returns immediately. New file -> 202 `processing` (ingestion runs in the background; poll
    GET /api/documents/{id}). Same bytes already ingested -> 200 `ready`, `cached: true`, nothing is re-processed.
    """
    engine = get_engine()
    filename = os.path.basename(file.filename or "").strip()[:200] or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only .pdf files are accepted.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.")
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(status_code=400, detail="File is not a valid PDF.")

    result, start_job = await asyncio.to_thread(_register_upload, data, filename)
    if start_job:
        background_tasks.add_task(jobs.run_ingest_job, result.document_id, result.filename, data, engine.sync.invalidate)
    elif result.status == "ready":
        response.status_code = 200
    return result

def _list_documents() -> list[DocumentInfo]:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute(f"{_DOC_SELECT} ORDER BY upload_timestamp DESC NULLS LAST LIMIT 200;")
        return [_doc_info(r) for r in cur.fetchall()]

def _get_document(doc_id: str) -> Optional[DocumentInfo]:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute(f"{_DOC_SELECT} WHERE id = %s;", (doc_id,))
        row = cur.fetchone()
    return _doc_info(row) if row else None

@app.get("/api/documents", response_model=List[DocumentInfo])
async def list_documents():
    """Newest first. The UI polls the single-document route below while one is processing."""
    return await asyncio.to_thread(_list_documents)

@app.get("/api/documents/{document_id}", response_model=DocumentInfo)
async def get_document(document_id: str):
    doc = await asyncio.to_thread(_get_document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return doc

def _delete_document(engine: HybridQueryEngine, doc_id: str) -> None:
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, storage_path FROM documents WHERE id = %s;", (doc_id,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        status, storage_path = row
        if status == "processing":
            raise HTTPException(status_code=409, detail="Document is still being processed.")

        # Neo4j first: if this fails the Postgres row survives and the delete can simply be retried.
        # Edges are stamped with doc_id; chunk/document nodes are removed with it; entities are shared across
        # documents, so only those no remaining chunk mentions are purged.
        with engine.neo4j_driver.session() as session:
            session.run("MATCH (:Entity)-[r]->(:Entity) WHERE r.doc_id = $id DELETE r", id=doc_id)
            session.run("MATCH (d:Document {id: $id}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c, d", id=doc_id)
        graph_sync.purge_orphans(engine.neo4j_driver)

        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = %s;", (doc_id,))  # chunks cascade; query_runs keep their history
        conn.commit()
    ingest.delete_pdf_blob(storage_path)
    engine.sync.invalidate(doc_id)

@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str):
    engine = get_engine()
    await asyncio.to_thread(_delete_document, engine, document_id)
    return {"deleted": document_id}

# ---------------------------------------------------------------------------
# 7. The f_api Endpoint (document-scoped) + the immutable run ledger
# ---------------------------------------------------------------------------

def _get_ready_document_name(document_id: str) -> str:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT filename, status FROM documents WHERE id = %s;", (document_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if row[1] != "ready":
        raise HTTPException(status_code=409, detail=f"Document is not ready (status: {row[1]}).")
    return row[0]

def _log_run(document_id: str, document_name: str, query: str, parameters: dict, payload: dict) -> Optional[str]:
    """Append to the query_runs ledger. A logging failure must never fail the query: returns None instead."""
    try:
        with pg_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO query_runs (document_id, document_name, query, parameters, response_payload) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING run_id;",
                (document_id, document_name, query, Json(parameters), Json(payload)))
            run_id = cur.fetchone()[0]
            conn.commit()
        return str(run_id)
    except Exception as e:
        print(f"[WARN] Could not log query run: {e}")
        return None

@app.post("/api/query", response_model=QueryResponse)
async def process_hybrid_query(request: QueryRequest):
    """
    Evaluates q -> v_q -> G_local -> C_LLM -> P_out, strictly within `request.document_id`.
    Every completed run is appended to the query_runs ledger (see GET /api/runs).
    """
    engine = get_engine()
    document_name = await asyncio.to_thread(_get_ready_document_name, request.document_id)

    try:
        # Pass the validated query state space to the engine (blocking: run off the event loop)
        raw_payload = await asyncio.to_thread(
            engine.execute_hybrid_query,
            request.query,
            request.document_id,
            k_hops=request.k_hops,
            top_k_chunks=request.top_k_chunks,
            temperature=request.temperature,
            top_p=request.top_p,
            strict_grounding=request.strict_grounding,
        )
        response = QueryResponse(**raw_payload, document_id=request.document_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Frozen record = exactly what the client receives, plus the effective (post-clamp) parameters
    if not response.answer.startswith("SYSTEM_ERROR"):
        t = response.telemetry
        parameters = {"k_hops": t.k_hops, "top_k_chunks": t.chunks_requested, "temperature": t.temperature,
                      "top_p": t.top_p, "strict_grounding": t.strict_grounding}
        response.run_id = await asyncio.to_thread(
            _log_run, request.document_id, document_name, response.query, parameters, response.model_dump(mode="json"))
    return response

_RUN_COLUMNS = ("run_id", "document_id", "document_name", "query", "parameters", "created_at")
_RUN_SUMMARY_COLS = (
    "run_id, document_id, document_name, query, parameters, created_at, "
    "(response_payload->'telemetry'->>'latency_ms')::float, (response_payload->'telemetry'->>'edge_count')::int"
)

def _run_summary(row) -> dict:
    out = dict(zip(_RUN_COLUMNS, row[:6]))
    out["run_id"] = str(out["run_id"])
    out["latency_ms"], out["edge_count"] = row[6], row[7]
    return out

def _list_runs(limit: int, before: Optional[datetime.datetime]) -> list[RunSummary]:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_RUN_SUMMARY_COLS} FROM query_runs "
                    "WHERE (%s::timestamptz IS NULL OR created_at < %s::timestamptz) "
                    "ORDER BY created_at DESC LIMIT %s;", (before, before, limit))
        return [RunSummary(**_run_summary(r)) for r in cur.fetchall()]

def _get_run(run_id: str) -> Optional[RunDetail]:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_RUN_SUMMARY_COLS}, response_payload FROM query_runs WHERE run_id = %s;", (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    payload = dict(row[8])
    payload["run_id"] = run_id
    return RunDetail(**_run_summary(row), response_payload=payload)

@app.get("/api/runs", response_model=List[RunSummary])
async def list_runs(limit: int = Query(50, ge=1, le=200), before: Optional[datetime.datetime] = None):
    """Chronological feed (newest first) across ALL documents; summaries only, no payloads.
    Page backwards by passing the oldest `created_at` received as `before`."""
    return await asyncio.to_thread(_list_runs, limit, before)

@app.get("/api/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID):
    """One frozen run, for rehydrating the UI: a single DB read, no retrieval and no LLM call."""
    run = await asyncio.to_thread(_get_run, str(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run

# Runs are never edited (a DB trigger rejects UPDATEs), but the history can be pruned explicitly.

def _delete_run(run_id: str) -> bool:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM query_runs WHERE run_id = %s;", (run_id,))
        deleted = cur.rowcount
        conn.commit()
    return deleted == 1

def _clear_runs() -> int:
    with pg_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM query_runs;")
        deleted = cur.rowcount
        conn.commit()
    return deleted

@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: uuid.UUID):
    """Remove one run from the history ledger."""
    if not await asyncio.to_thread(_delete_run, str(run_id)):
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"deleted": str(run_id)}

@app.delete("/api/runs")
async def clear_runs():
    """Clear the whole history ledger (every document). Irreversible; the UI asks for confirmation first."""
    return {"deleted": await asyncio.to_thread(_clear_runs)}

# ---------------------------------------------------------------------------
# 6. Graph sync (Neo4j projection vs Supabase extraction cache)
# ---------------------------------------------------------------------------

class SyncRequest(BaseModel):
    # project: free (0 LLM tokens), re-project drifted chunks from the cache
    # extract: LLM extraction for ONLY the chunks whose cache is missing/stale, then project
    mode: Literal["project", "extract"] = "project"

@app.get("/api/graph/sync")
async def graph_sync_status(refresh: bool = False, document_id: Optional[str] = None):
    """Cheap consistency check (no LLM). `refresh=true` bypasses the TTL cache; `document_id` scopes it to one document."""
    engine: HybridQueryEngine = engine_instance.get("engine")
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    try:
        return engine.sync.status(engine.pg_conn, engine.neo4j_driver, force=refresh, doc_id=document_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/graph/sync")
async def graph_sync_heal(request: SyncRequest):
    engine: HybridQueryEngine = engine_instance.get("engine")
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")

    def run():
        # Dedicated Postgres connection: the engine's one may be serving a query
        pg = get_postgres_connection()
        if pg is None:
            raise RuntimeError("Could not open a Postgres connection.")
        try:
            return engine.sync.heal(pg, engine.neo4j_driver, request.mode)
        finally:
            pg.close()

    try:
        result = await asyncio.to_thread(run)
        return {"mode": request.mode, "result": result, "status": engine.sync.status(engine.pg_conn, engine.neo4j_driver, force=True)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # Execute the server on localhost:8000 (watch only Python source dirs so StatReload ignores .venv and node_modules)
    watch_dirs = [str(PROJECT_ROOT / d) for d in ("server_api", "retrieval", "pipeline", "database", "ontologies")]
    uvicorn.run("server_api.main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=watch_dirs, app_dir=str(PROJECT_ROOT))
