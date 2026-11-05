import os
import sys
import time
import json
import argparse
import hashlib
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

from google import genai
from google.genai import types
from google.genai.errors import APIError

from neo4j import GraphDatabase
from pydantic import BaseModel, Field, ValidationError

from database import get_postgres_connection, get_neo4j_driver
from ontologies import PROMPT
from pipeline import graph_sync

load_dotenv(override=True)

TARGET_EXTRACTION_MODEL = os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-3.5-flash-lite")
BATCH_SIZE = 4


# --- 1. PYDANTIC SCHEMA DEFINITION ---
class MetaRelationType(str, Enum):
    # Dynamic Forces (Ontogenesis & Change)
    ACTUALIZES = "ACTUALIZES"
    MODULATES = "MODULATES"
    POTENTIATES = "POTENTIATES"

    # Structural Forces (Mereology & Boundary)
    COMPOSES = "COMPOSES"
    CONSTRAINS = "CONSTRAINS"
    DIFFERENTIATES = "DIFFERENTIATES"

    # Associative Mappings (Category Theory Functors)
    MAPS_TO = "MAPS_TO"


class Entity(BaseModel):
    name: str = Field(description="Unique capitalized canonical name of the entity.")
    entity_type: str = Field(description="Dynamic category of the entity (e.g. METHOD, HYPERPARAMETER, DATASET).")
    description: str = Field(description="Precise description based on the text.")


class Relationship(BaseModel):
    source: str = Field(description="Canonical name of source entity.")
    target: str = Field(description="Canonical name of target entity.")
    relation_type: MetaRelationType = Field(
        description="Strict meta-relation classification from the allowed enumeration."
    )
    detail: str = Field(
        description="Specific, highly descriptive technical interaction/mechanism connecting source to target."
    )
    confidence: float = Field(
        description="Confidence scalar between 0.0 and 1.0."
    )


# Preserved for database compatibility
class GraphExtraction(BaseModel):
    ontology_hash: str | None = Field(
        default=None,
        description="Cryptographic fingerprint of the ontology prompt used for extraction."
    )
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


# New wrappers for batched LLM constraint enforcement
class ChunkGraphExtraction(BaseModel):
    chunk_id: str = Field(description="The exact chunk identifier provided in the input.")
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class BatchedGraphExtraction(BaseModel):
    batch: list[ChunkGraphExtraction] = Field(
        description="List of extracted sub-graphs, exactly one per input chunk."
    )


DYNAMIC_SYSTEM_PROMPT = PROMPT + f"""

=========================================
CRITICAL STRUCTURAL CONSTRAINTS:
You MUST output valid JSON strictly conforming to the JSON schema.
1. `relation_type` MUST be exactly chosen from the defined Enum list. Do NOT invent new relationships.
2. You MUST provide the `detail` string for every relationship.
3. You will receive a batch of text chunks separated by boundaries. You MUST isolate the extraction per chunk and include the EXACT `chunk_id` in your response. Do not cross-pollinate entities or relationships across chunks.
4. Analyze the provided text chunks to identify the active forces, dependencies, and structural morphisms between entities. Map these interactions exclusively to the provided basis vectors. Entities are autonomous holons; you must not infer forces across chunk boundaries unless explicitly stated in the text.

SCHEMA:
{json.dumps(BatchedGraphExtraction.model_json_schema(), indent=2)}
"""

# 1. Compute deterministic prompt fingerprint
CURRENT_ONTOLOGY_HASH = hashlib.sha256(DYNAMIC_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]


# --- 2. BATCHED EXTRACTION LOGIC (GEMINI & STRUCTURED OUTPUTS) ---
def extract_batched_topology(client: genai.Client, text: str, max_retries: int = 3) -> BatchedGraphExtraction:
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=BatchedGraphExtraction,
        system_instruction=DYNAMIC_SYSTEM_PROMPT,
        temperature=0.0
    )

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=TARGET_EXTRACTION_MODEL,
                contents=text,
                config=config
            )
            return BatchedGraphExtraction.model_validate_json(response.text)

        except APIError as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str:
                wait_time = 10 * (attempt + 1)
                print(f"  [WARN] 429 Rate Limit. Pausing {wait_time}s...")
                time.sleep(wait_time)
            elif "403" in error_str or "404" in error_str:
                print(f"\n[FATAL] Global Boundary Violation (Auth/Permissions): {e}")
                sys.exit(1)
            else:
                if attempt < max_retries - 1:
                    time.sleep(5)
                else:
                    raise e
        except ValidationError as ve:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise ve
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                raise e


# --- 3. PARALLEL WORKER FUNCTION ---
def process_chunk_batch(client: genai.Client, chunk_batch: list[tuple]) -> list[tuple]:
    """
    Maps: (chunk_id, doc_id, text) -> (chunk_id, doc_id, text, GraphExtraction | None, Exception | None)
    Maps a list of input text tuples into a single LLM prompt space,
    then demultiplexes the structural results back into individual graphs.
    """
    prompt_text = ""
    for chunk_id, doc_id, text in chunk_batch:
        prompt_text += f"\n--- CHUNK START: {chunk_id} ---\n{text}\n--- CHUNK END ---\n"

    try:
        batched_data = extract_batched_topology(client, prompt_text)

        results = []
        extracted_dict = {item.chunk_id: item for item in batched_data.batch}

        for chunk_id, doc_id, text in chunk_batch:
            if chunk_id in extracted_dict:
                item = extracted_dict[chunk_id]
                graph_data = GraphExtraction(
                    ontology_hash=CURRENT_ONTOLOGY_HASH,
                    entities=item.entities,
                    relationships=item.relationships,
                )
                results.append((chunk_id, doc_id, text, graph_data, None))
            else:
                results.append((chunk_id, doc_id, text, None, Exception(f"Model omitted chunk_id {chunk_id} from batch output.")))
        return results
    except Exception as e:
        return [(chunk_id, doc_id, text, None, e) for chunk_id, doc_id, text in chunk_batch]


# --- 4. NEO4J PROJECTION (see pipeline/graph_sync.py) ---
# Neo4j is a deterministic projection of the Supabase `extracted_graph` cache: each chunk is projected in one
# transaction with chunk_id provenance on every edge, so re-extraction REPLACES a chunk's edges instead of
# accumulating stale ones. Drift detection / repair (check_sync, heal_projection) lives in graph_sync.
def ingest_chunk_to_neo4j(driver, chunk_id: str, doc_id: str, text_content: str, graph: GraphExtraction):
    proj = graph_sync.expected_projection(graph.model_dump(mode="json"), doc_id)
    graph_sync.project_chunk(driver, chunk_id, doc_id, text_content, proj)


# --- 5. PIPELINE STAGE HELPERS ---
def _ensure_neo4j_schema(neo4j_driver) -> None:
    """Create primary key topological constraints and fulltext indexes in Neo4j if not present."""
    with neo4j_driver.session() as session:
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE")
        session.run("""
            CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS 
            FOR (c:Chunk) ON EACH [c.text]
        """)
        session.run("""
            CREATE FULLTEXT INDEX entity_semantic_index IF NOT EXISTS 
            FOR (e:Entity) ON EACH [e.name, e.description]
        """)


def _fetch_target_chunks(pg_cursor, doc_id: str | None, limit: int | None) -> list[tuple]:
    """Fetch (id, document_id, text_content, extracted_graph) tuples from PostgreSQL."""
    sql = "SELECT id, document_id, text_content, extracted_graph FROM document_chunks"
    sql_params: list = []
    if doc_id is not None:
        sql += " WHERE document_id = %s"
        sql_params.append(doc_id)
    sql += " ORDER BY document_id, chunk_index ASC"
    if limit is not None:
        sql += " LIMIT %s"
        sql_params.append(limit)
    pg_cursor.execute(sql, sql_params)
    return pg_cursor.fetchall()


def _partition_cache_hits(chunks: list[tuple], force_reextract: bool) -> tuple[list[tuple], list[tuple]]:
    """Separate valid cache hits (matching CURRENT_ONTOLOGY_HASH) from chunks requiring LLM extraction."""
    ready_graphs = []
    to_extract = []
    for chunk_id, chunk_doc_id, text, cached_graph in chunks:
        if cached_graph and not force_reextract:
            try:
                parsed_graph = GraphExtraction.model_validate(cached_graph)
                if parsed_graph.ontology_hash == CURRENT_ONTOLOGY_HASH:
                    ready_graphs.append((chunk_id, chunk_doc_id, text, parsed_graph))
                else:
                    print(f" [CACHE INVALID] {chunk_id}: Prompt signature mismatch ({parsed_graph.ontology_hash} != {CURRENT_ONTOLOGY_HASH}). Re-extracting...")
                    to_extract.append((chunk_id, chunk_doc_id, text))
            except ValidationError:
                to_extract.append((chunk_id, chunk_doc_id, text))
        else:
            to_extract.append((chunk_id, chunk_doc_id, text))
    return ready_graphs, to_extract


def _extract_missing_batches(
    gemini_client: genai.Client,
    pg_conn,
    pg_cursor,
    to_extract: list[tuple],
    max_workers: int,
    on_batch_done,
) -> tuple[list[tuple], int]:
    """Execute concurrent batched LLM extraction and persist JSONB caches to PostgreSQL."""
    extracted_graphs = []
    failed = 0
    batches = [to_extract[i:i + BATCH_SIZE] for i in range(0, len(to_extract), BATCH_SIZE)]

    print(f"[INFO] Dispatching {len(batches)} batched API extraction threads (k={BATCH_SIZE})...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(process_chunk_batch, gemini_client, batch): batch
            for batch in batches
        }
        for future in as_completed(future_to_batch):
            for chunk_id, chunk_doc_id, text, graph_data, error in future.result():
                if error:
                    print(f"  [ERROR] Extraction failed for {chunk_id}: {error}")
                    failed += 1
                    continue
                if graph_data:
                    print(f"  [SUCCESS] {chunk_id} -> {len(graph_data.entities)} entities, {len(graph_data.relationships)} relationships")
                    extracted_graphs.append((chunk_id, chunk_doc_id, text, graph_data))
                    pg_cursor.execute(
                        "UPDATE document_chunks SET extracted_graph = %s::jsonb WHERE id = %s;",
                        (graph_data.model_dump_json(), chunk_id),
                    )
                    pg_conn.commit()
            on_batch_done(len(extracted_graphs), failed)

    print(f"[INFO] Parallel batched extraction converged in {time.time() - start_time:.2f} seconds.")
    return extracted_graphs, failed


# --- 6. MAIN PIPELINE EXECUTION ---
def run_pipeline(limit: int | None = 35, force_reextract: bool = False, max_workers: int = 15,
                 doc_id: str | None = None, on_progress=None) -> dict:
    """Extract (cache-first) and project the graph for `doc_id` (None = every document).

    limit: max chunks to evaluate (None = all of them). on_progress(done, total) is called as chunks complete.
    Returns {"total", "cached", "extracted", "failed"} so callers (pipeline/jobs.py) can report the outcome."""
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not gemini_key:
        raise ValueError("Missing GOOGLE_API_KEY or GEMINI_API_KEY in environment.")
    gemini_client = genai.Client(api_key=gemini_key)

    neo4j_driver = get_neo4j_driver()
    pg_conn = get_postgres_connection()
    pg_cursor = pg_conn.cursor()

    try:
        _ensure_neo4j_schema(neo4j_driver)
        chunks = _fetch_target_chunks(pg_cursor, doc_id, limit)
        print(f"[INFO] Evaluating {len(chunks)} document chunks...")

        ready_graphs, to_extract = _partition_cache_hits(chunks, force_reextract)
        cached_count = len(ready_graphs)
        failed = 0
        if on_progress:
            on_progress(cached_count, len(chunks))

        if to_extract:
            def _on_batch(extracted_so_far: int, failed_so_far: int):
                if on_progress:
                    on_progress(cached_count + extracted_so_far + failed_so_far, len(chunks))

            new_graphs, failed = _extract_missing_batches(
                gemini_client, pg_conn, pg_cursor, to_extract, max_workers, _on_batch
            )
            ready_graphs.extend(new_graphs)

        print("[INFO] Verifying Neo4j projection against the Supabase cache...")
        neo4j_start = time.time()
        report = graph_sync.check_sync(pg_conn, neo4j_driver, doc_id)
        if report["status"] == "in_sync" and not force_reextract:
            print(f"[INFO] Neo4j topology is fully synchronized ({report['chunks_total']} chunks, check {report['check_ms']} ms).")
        else:
            print(f"[INFO] Projecting: {len(report['drift'])} drifted chunk(s), {report['orphan_edges']} orphan edge(s), "
                  f"{report['orphan_entities']} orphan entit(ies)" + (" [forced: all chunks]" if force_reextract else ""))
            result = graph_sync.heal_projection(pg_conn, neo4j_driver, report, force_all=force_reextract)
            print(f"[SUCCESS] Neo4j projection converged in {time.time() - neo4j_start:.2f} seconds: {result}")

        print(f"[SUCCESS] Pipeline converged. Ingested {len(ready_graphs)}/{len(chunks)} chunks.")
        return {"total": len(chunks), "cached": cached_count, "extracted": len(ready_graphs) - cached_count, "failed": failed}

    except Exception as e:
        print(f"[FATAL] Pipeline halted: {e}")
        raise e
    finally:
        pg_cursor.close()
        pg_conn.close()
        neo4j_driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Graph Assembly & Ingestion Engine")
    parser.add_argument("--limit", type=int, default=35, help="Max chunks to process.")
    parser.add_argument("--force", action="store_true", help="Force re-extraction via LLM, ignoring Supabase cache.")
    parser.add_argument("--workers", type=int, default=15, help="Thread pool concurrency limit.")
    args = parser.parse_args()

    run_pipeline(limit=args.limit, force_reextract=args.force, max_workers=args.workers)
