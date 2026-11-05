import hashlib
import io
import os
import re
import datetime
from pypdf import PdfReader
from psycopg2.extras import execute_values
from typing import List, Optional

from database.database import get_postgres_connection
from dotenv import load_dotenv

# Supabase REST Client
from supabase import create_client, Client

# Initialize environment space
load_dotenv(override=True)

# Initialize Supabase Client (Requires SERVICE_ROLE_KEY for bucket creation)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None
    print("[WARN] Supabase URL or Key missing. Blob storage operations will be bypassed.")

BUCKET_NAME = "documents"
# Upper bound on chunks per document: every chunk costs embedding + LLM extraction calls
MAX_CHUNKS_PER_DOC = int(os.getenv("MAX_CHUNKS_PER_DOC", "300"))


# ==========================================
# 0. Document Identity
# ==========================================

def compute_doc_id(data: bytes) -> str:
    """A document's identity is the SHA-256 of its bytes: re-uploading the same file is a cache hit."""
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str) -> str:
    """Storage-path-safe basename (no directories, no exotic characters)."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name or "")).strip("._")
    return (base or "document.pdf")[:120]


# ==========================================
# 1. Deterministic PDF Parsing & Chunking
# ==========================================

def parse_pdf_bytes(data: bytes) -> str:
    """Extract all text from PDF bytes deterministically. Raises ValueError for unreadable PDFs."""
    try:
        reader = PdfReader(io.BytesIO(data))
        full_text = ""
        for page in reader.pages:
            # We extract text, adding a space to ensure word boundaries are respected
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + " "
        return full_text.strip()
    except Exception as e:
        raise ValueError(f"Could not read PDF: {e}") from e


def parse_pdf(file_path: str) -> str:
    """
    Parses a PDF file and extracts all text deterministically.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Cannot locate PDF at {file_path}")

    try:
        with open(file_path, "rb") as f:
            return parse_pdf_bytes(f.read())
    except ValueError as e:
        print(f"Error parsing PDF: {e}")
        return ""


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    """
    Implements a sliding window chunking algorithm.
    Maps a continuous string into a finite set of overlapping segments.
    """
    if not text:
        return []

    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        # Define the end of the current chunk
        end = start + chunk_size

        # If we are not at the end of the text, try to find a natural break point (like a period or space)
        # to avoid slicing words in half. We look backwards from our target 'end'.
        if end < text_length:
            # Try to find a period within the last 50 characters of the chunk
            natural_break = text.rfind('.', start, end)
            if natural_break != -1 and natural_break > start + (chunk_size // 2):
                end = natural_break + 1  # Include the period
            else:
                # Fallback to nearest space
                natural_break = text.rfind(' ', start, end)
                if natural_break != -1:
                    end = natural_break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Advance the sliding window, stepping back by the overlap. Never step backwards: a break point
        # closer than `overlap` to `start` (e.g. a single space in a long unbroken run) would otherwise
        # loop forever, which matters now that the input is untrusted uploads.
        next_start = end - overlap
        start = next_start if next_start > start else max(end, start + 1)

    return chunks


# ==========================================
# 2. Database Insertion (Topology Mapping)
# ==========================================

def register_document(conn, doc_id: str, filename: str, size_bytes: Optional[int] = None) -> bool:
    """
    Inserts the root document row (status 'processing'). Returns True if it was created, False if it
    already existed. Raises on database errors. `content_hash` mirrors the SHA-256 id (dedup lookup key).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (id, filename, upload_timestamp, content_hash, size_bytes, status)
            VALUES (%s, %s, %s, %s, %s, 'processing')
            ON CONFLICT (id) DO NOTHING;
            """,
            (doc_id, filename, datetime.datetime.now(datetime.timezone.utc), doc_id, size_bytes)
        )
        created = cur.rowcount == 1
    conn.commit()
    return created


def insert_chunks_pg(conn, doc_id: str, chunks: List[str]) -> None:
    """
    Bulk-inserts a document's chunks in ONE transaction. `embedding` stays NULL until sync_embeddings runs,
    so a chunk can never be returned by vector search before it has a real vector.
    """
    rows = [(f"{doc_id}_chunk_{i}", doc_id, i, content) for i, content in enumerate(chunks)]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO document_chunks (id, document_id, chunk_index, text_content)
            VALUES %s
            ON CONFLICT (id) DO NOTHING;
            """,
            rows
        )
    conn.commit()


# ==========================================
# 2.5 Blob Storage Boundary Integration
# ==========================================

def upload_pdf_bytes(data: bytes, doc_id: str, filename: str) -> str:
    """
    Mechanically verifies bucket boundary existence, creates it if necessary,
    and transitions the binary file into the object store. Returns the storage path ("" on failure).
    """
    if not supabase:
        return ""

    # 1. State check: evaluate if the bucket boundary exists
    try:
        supabase.storage.get_bucket(BUCKET_NAME)
    except Exception:
        print(rf"  [INFO] Boundary '{BUCKET_NAME}' \notin \mathcal{{S}}. Instantiating bucket...")
        # Requires SERVICE_ROLE_KEY to execute successfully
        supabase.storage.create_bucket(BUCKET_NAME, options={"public": False})

    # 2. Transition file into bucket
    storage_path = f"{doc_id}/{safe_filename(filename)}"
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            storage_path,
            data,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )
        print(f"  [SUPABASE] Binary successfully mapped to storage path: {storage_path}")
        return storage_path
    except Exception as e:
        print(f"  [ERROR] Blob transition failed: {e}")
        return ""


def ensure_bucket_and_upload(file_path: str, doc_id: str, filename: str) -> str:
    """File-path wrapper around upload_pdf_bytes (CLI path)."""
    with open(file_path, "rb") as f:
        return upload_pdf_bytes(f.read(), doc_id, filename)


def download_pdf_bytes(storage_path: str) -> Optional[bytes]:
    """Fetch a stored PDF (used to resume an interrupted ingestion). None if unavailable."""
    if not supabase or not storage_path:
        return None
    try:
        return supabase.storage.from_(BUCKET_NAME).download(storage_path)
    except Exception as e:
        print(f"  [ERROR] Blob download failed for {storage_path}: {e}")
        return None


def delete_pdf_blob(storage_path: str) -> None:
    """Remove a stored PDF (best effort)."""
    if not supabase or not storage_path:
        return
    try:
        supabase.storage.from_(BUCKET_NAME).remove([storage_path])
    except Exception as e:
        print(f"  [WARN] Blob delete failed for {storage_path}: {e}")


# ==========================================
# 3. Ingestion
# ==========================================

def ingest_pdf_bytes(conn, doc_id: str, data: bytes, max_chunks: int = MAX_CHUNKS_PER_DOC) -> int:
    """
    Parse + chunk + insert one document's chunks (embeddings NULL). Idempotent: if the document already has
    chunks, nothing is re-parsed. Returns the chunk count. Raises ValueError for unusable PDFs.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM document_chunks WHERE document_id = %s;", (doc_id,))
        existing = cur.fetchone()[0]
    if existing:
        return existing

    raw_text = parse_pdf_bytes(data)
    if not raw_text:
        raise ValueError("No extractable text found (scanned or image-only PDF?).")

    chunks = chunk_text(raw_text, chunk_size=1000, overlap=200)
    if len(chunks) > max_chunks:
        raise ValueError(f"Document too large: {len(chunks)} chunks exceeds the limit of {max_chunks}.")

    insert_chunks_pg(conn, doc_id, chunks)
    return len(chunks)


def run_ingestion_pipeline(pdf_file_path: str) -> Optional[str]:
    """
    CLI ingestion (stage 1 of run_pipeline.py): identity -> blob -> parse/chunk -> chunk rows.
    Returns the document id (SHA-256 of the file bytes), or None on failure. The API path uses
    pipeline/jobs.py instead, which also tracks status.
    """
    print(f"Starting ingestion pipeline for: {pdf_file_path}")

    print("1. Connecting to state space and resolving identity...")
    pg_conn = get_postgres_connection()
    if not pg_conn:
        print("[ERROR] Database connection failed. Aborting ingestion.")
        return None

    try:
        with open(pdf_file_path, "rb") as f:
            data = f.read()
        filename = os.path.basename(pdf_file_path)
        doc_id = compute_doc_id(data)

        # Computational idempotency: the same bytes always map to the same document
        if not register_document(pg_conn, doc_id, filename, len(data)):
            print(f"  [CACHE HIT] Document '{filename}' ({doc_id[:12]}...) already exists. Bypassing parser.")

        print("2. Pushing binary object to Supabase Storage...")
        storage_path = upload_pdf_bytes(data, doc_id, filename)
        if storage_path:
            with pg_conn.cursor() as cur:
                cur.execute("UPDATE documents SET storage_path = %s WHERE id = %s;", (storage_path, doc_id))
            pg_conn.commit()

        print("3. Extracting, chunking and inserting text...")
        count = ingest_pdf_bytes(pg_conn, doc_id, data)
        print(f"  {count} chunks stored for {doc_id[:12]}...")
        print("[SUCCESS] Ingestion complete!")
        return doc_id
    except Exception as e:
        pg_conn.rollback()
        print(f"[ERROR] Ingestion failed: {e}")
        return None
    finally:
        pg_conn.close()


if __name__ == "__main__":
    # Candidate paths to locate test_document.pdf across directories
    possible_paths = [
        "test_document.pdf",
        os.path.join("data", "test_document.pdf"),
        os.path.join("..", "data", "test_document.pdf"),
    ]

    target_pdf = next((p for p in possible_paths if os.path.exists(p)), None)

    if target_pdf:
        print(f"[INFO] Located PDF at: {target_pdf}")
        run_ingestion_pipeline(target_pdf)
    else:
        print("[ERROR] Could not find 'test_document.pdf' in root or data directories.")
