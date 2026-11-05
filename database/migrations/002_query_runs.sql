-- 002_query_runs.sql: immutable ledger of every completed /api/query run
--
-- document_id is ON DELETE SET NULL and document_name is a snapshot, so deleting a PDF keeps its history.
-- The trigger makes rows append-only: the single permitted UPDATE is the FK action nulling document_id.
-- NOTE: TRUNCATE documents CASCADE also empties this table; use DELETE FROM documents (database/reset_system.py does).

BEGIN;

CREATE TABLE IF NOT EXISTS query_runs (
    run_id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      varchar     REFERENCES documents(id) ON DELETE SET NULL,
    document_name    text        NOT NULL,
    query            text        NOT NULL,
    parameters       jsonb       NOT NULL,
    response_payload jsonb       NOT NULL,
    schema_version   integer     NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_runs_created_idx ON query_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS query_runs_document_idx ON query_runs (document_id);

CREATE OR REPLACE FUNCTION query_runs_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.document_id IS NOT NULL
       OR (to_jsonb(NEW) - 'document_id') IS DISTINCT FROM (to_jsonb(OLD) - 'document_id') THEN
        RAISE EXCEPTION 'query_runs is an immutable ledger (only document_id may be nulled, when its document is deleted)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS query_runs_no_update ON query_runs;
CREATE TRIGGER query_runs_no_update
    BEFORE UPDATE ON query_runs
    FOR EACH ROW EXECUTE FUNCTION query_runs_immutable();

COMMIT;
