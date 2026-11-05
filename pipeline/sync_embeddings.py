import argparse
import hashlib
import os
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from google import genai
from google.genai import types

from database import get_postgres_connection

# Force environment state override to invalidate stale system variables
load_dotenv(override=True)

TARGET_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

# Asymmetric retrieval: chunks are embedded as RETRIEVAL_DOCUMENT, queries (query_engine.py) as RETRIEVAL_QUERY.
# The tag stored in embedding_model includes the task type, so vectors produced without it
# (or with another model) are detected as stale and re-embedded by a plain sync run.
EMBEDDING_TASK = "RETRIEVAL_DOCUMENT"
EMBEDDING_TAG = f"{TARGET_MODEL}:{EMBEDDING_TASK}"


def compute_sha256(text: str) -> str:
    """Computes a deterministic hash of the chunk text payload."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fetch_stale_chunks(cursor, doc_id: str | None, force: bool) -> list[tuple[str, str]]:
    """Return (chunk_id, text_content) rows requiring vector projection."""
    scope_sql = " AND document_id = %s" if doc_id is not None else ""
    scope_params = (doc_id,) if doc_id is not None else ()
    if force:
        print("[INFO] --force flag detected. Invalidating all existing vector projections...")
        cursor.execute(f"SELECT id, text_content FROM document_chunks WHERE TRUE{scope_sql}", scope_params)
    else:
        cursor.execute(
            f"""
            SELECT id, text_content
            FROM document_chunks
            WHERE (embedding IS NULL
               OR embedding_model IS NULL
               OR embedding_model != %s){scope_sql};
            """,
            (EMBEDDING_TAG, *scope_params),
        )
    return cursor.fetchall()


def _embed_chunk_batch(client: genai.Client, batch: list[tuple[str, str]]) -> list[tuple]:
    """Project a batch of (chunk_id, text) rows into 768-d vectors and return bulk-update tuples."""
    update_payloads = []
    for chunk_id, text in batch:
        content_hash = compute_sha256(text)
        response = client.models.embed_content(
            model=TARGET_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=768, task_type=EMBEDDING_TASK),
        )
        vector = response.embeddings[0].values
        update_payloads.append((vector, content_hash, EMBEDDING_TAG, chunk_id))
    return update_payloads


def synchronize_vectors(force: bool = False, batch_size: int = 50, doc_id: str | None = None, on_progress=None):
    """Embed stale/missing chunks (of one document if doc_id is given, else every document).
    on_progress(done, total) is called after each committed batch."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Neither GEMINI_API_KEY nor GOOGLE_API_KEY found in environment state.")

    client = genai.Client(api_key=api_key)
    conn = get_postgres_connection()
    cursor = conn.cursor()

    try:
        stale_chunks = _fetch_stale_chunks(cursor, doc_id, force)
        if not stale_chunks:
            print("[INFO] State Space Converged: All vectors are up-to-date.")
            return

        print(f"[INFO] Processing {len(stale_chunks)} chunks using '{EMBEDDING_TAG}'...")
        update_query = """
            UPDATE document_chunks AS d SET
                embedding = v.vec::vector,
                content_hash = v.hash,
                embedding_model = v.model
            FROM (VALUES %s) AS v(vec, hash, model, id)
            WHERE d.id = v.id;
        """

        for i in range(0, len(stale_chunks), batch_size):
            batch = stale_chunks[i:i + batch_size]
            update_payloads = _embed_chunk_batch(client, batch)
            execute_values(cursor, update_query, update_payloads, template=None)
            conn.commit()
            print(f"[INFO] Batch {i // batch_size + 1}/{(len(stale_chunks) - 1) // batch_size + 1} committed successfully.")
            if on_progress:
                on_progress(min(i + batch_size, len(stale_chunks)), len(stale_chunks))

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Transaction aborted. State rolled back: {e}")
        raise e
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vector Projection Synchronization Engine")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-embedding of all document chunks regardless of current state.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of vectors to process per batch transaction.",
    )
    args = parser.parse_args()

    synchronize_vectors(force=args.force, batch_size=args.batch_size)
