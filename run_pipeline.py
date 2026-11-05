import os
import subprocess
import sys
import argparse
from pathlib import Path

_VENV_PY = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe" #edit `.venv` accordingly if needed.
if __name__ == "__main__" and _VENV_PY.exists() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    raise SystemExit(subprocess.call([str(_VENV_PY), *sys.argv]))

from database.init_neo4j_schema import initialize_neo4j_schema
from pipeline.ingest import run_ingestion_pipeline
from pipeline.sync_embeddings import synchronize_vectors
from pipeline.assemble_graph import run_pipeline as run_assemble_graph

from database.database import get_postgres_connection
from database.reset_system import reset_neo4j, reset_supabase


def mark_document_ready(doc_id: str) -> None:
    conn = get_postgres_connection()
    if conn is None:
        raise RuntimeError("Could not open a Postgres connection to mark the document ready.")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET status = 'ready', stage = NULL, error = NULL, updated_at = now() WHERE id = %s;", (doc_id,))
        conn.commit()
    finally:
        conn.close()


def execute_master_pipeline(pdf_path: str, limit_chunks: int = 35,
                            force_graph: bool = False, force_vectors: bool = False, reset_all: bool = False):
    print("==================================================")
    print("      STARTING HYBRID GRAPH-RAG INGESTION         ")
    print("==================================================")

    # 0. Evaluate State Space Purge Conditions
    if reset_all:
        print("\n[SYSTEM RESET] --reset-all detected. Purging Supabase and Neo4j spaces...")
        reset_supabase()
        reset_neo4j()
    elif force_graph:
        print("\n[GRAPH RESET] --force-graph detected. Purging Neo4j topology only...")
        reset_neo4j()

    # 1. Ensure Neo4j Schema Constraints exist
    print("\n[STEP 1/4] Verifying Neo4j Schema Constraints...")
    initialize_neo4j_schema()

    # 2. Parse PDF & Ingest Raw Chunks to Supabase / Neo4j
    print(f"\n[STEP 2/4] Ingesting & Chunking Document: {pdf_path}")
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Target PDF not found at: {pdf_path}")
    doc_id = run_ingestion_pipeline(pdf_path)  # SHA-256 of the file bytes
    if doc_id is None:
        raise RuntimeError(f"Ingestion failed for {pdf_path}; see the log above.")

    # 3. Generate & Synchronize 768d Vector Embeddings (this document only)
    print("\n[STEP 3/4] Synchronizing Vector Space Embeddings...")
    synchronize_vectors(force=force_vectors, doc_id=doc_id)

    # 4. Extract Entities & Construct Knowledge Graph (this document only)
    print("\n[STEP 4/4] Assembling Knowledge Graph & Bipartite Edges...")
    run_assemble_graph(limit=limit_chunks, force_reextract=(force_graph or reset_all), doc_id=doc_id)

    # The API only lets queries see documents whose ingestion finished
    mark_document_ready(doc_id)

    print("\n==================================================")
    print("   [SUCCESS] PIPELINE COMPLETE: DATA FULLY READY  ")
    print("==================================================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Master Ingestion Orchestrator")
    parser.add_argument(
        "--pdf",
        type=str,
        default="data/test_document.pdf",
        help="Path to the source PDF file."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=35,
        help="Maximum chunk limit for graph extraction."
    )
    parser.add_argument(
        "--force-graph",
        action="store_true",
        help="Purge Neo4j, bypass Supabase JSONB cache, and force re-extraction via Groq."
    )
    parser.add_argument(
        "--force-vectors",
        action="store_true",
        help="Force re-embedding of chunks in Supabase via Gemini API."
    )
    parser.add_argument(
        "--reset-all",
        action="store_true",
        help="TRUNCATE Supabase, purge Neo4j, and re-run the entire pipeline from scratch."
    )
    args = parser.parse_args()

    execute_master_pipeline(
        pdf_path=args.pdf,
        limit_chunks=args.limit,
        force_graph=args.force_graph,
        reset_all=args.reset_all,
        force_vectors=args.force_vectors
    )
