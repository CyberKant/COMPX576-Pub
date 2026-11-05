-- 001_multidoc.sql: multi-document support (documents lifecycle + chunk partitioning)
--
-- DESTRUCTIVE ON FIRST RUN: truncates `documents` and (via CASCADE) `document_chunks`.
-- Decision: the legacy single test document is not migrated; re-upload it through the UI.
-- Pair with a Neo4j purge:  python -m database.reset_system --target neo4j --yes
-- The truncate is guarded, so re-running this file on an already-migrated database wipes nothing.
-- Apply BEFORE 002_query_runs.sql (TRUNCATE ... CASCADE would also empty a query_runs ledger).

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'documents' AND column_name = 'content_hash'
    ) THEN
        TRUNCATE TABLE documents CASCADE;
    END IF;
END $$;

-- documents: ingestion lifecycle. `id` is the SHA-256 hex digest of the PDF bytes (set by the API);
-- `content_hash` mirrors it and is the dedup lookup key.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS content_hash text UNIQUE,
    ADD COLUMN IF NOT EXISTS status       text NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'ready', 'failed')),
    ADD COLUMN IF NOT EXISTS stage        text,
    ADD COLUMN IF NOT EXISTS chunks_total integer,
    ADD COLUMN IF NOT EXISTS chunks_done  integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS error        text,
    ADD COLUMN IF NOT EXISTS storage_path text,
    ADD COLUMN IF NOT EXISTS size_bytes   bigint,
    ADD COLUMN IF NOT EXISTS updated_at   timestamptz NOT NULL DEFAULT now();

-- document_chunks: every chunk belongs to exactly one document
ALTER TABLE document_chunks ALTER COLUMN document_id SET NOT NULL;

-- Chunks are inserted before they are embedded. NULL (rather than a zero-vector placeholder, which has an
-- undefined cosine distance) keeps them out of vector search; queries already filter `embedding IS NOT NULL`.
ALTER TABLE document_chunks ALTER COLUMN embedding DROP NOT NULL;

CREATE INDEX IF NOT EXISTS document_chunks_document_idx ON document_chunks (document_id, chunk_index);

COMMIT;
