"""
Cache-first graph synchronisation.

The Supabase `document_chunks.extracted_graph` JSONB (stamped with the ontology prompt hash) is the
source of truth. Neo4j is a deterministic *projection* of it:

    expected_projection(cache)  ->  entities + thresholded edges + graph_hash
    project_chunk(...)          ->  writes that projection for ONE chunk in ONE transaction
                                    (every entity->entity edge carries `chunk_id` provenance)

Because a projection can be verified and rebuilt without an LLM, drift is repaired for free:

    L0  in sync              -> nothing
    L1  projection drift     -> re-project drifted chunks from the cache, purge orphans (0 tokens)
    L2  cache stale/missing  -> batched LLM extraction for ONLY those chunks, then L1 (tokens)

`check_sync` costs ~2 small queries. `GraphSyncManager` wraps it with a TTL for the API. L2 is never
run implicitly unless AUTO_HEAL_EXTRACT=1.
"""
import collections
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional


# Minimum confidence for an edge to be projected into Neo4j (part of the projection hash)
THRESHOLD_MAP = {
    "ACTUALIZES": 0.80, "MODULATES": 0.80, "POTENTIATES": 0.80,
    "COMPOSES": 0.70, "CONSTRAINS": 0.70, "DIFFERENTIATES": 0.70,
    "MAPS_TO": 0.55,
}
# Whitelist: relationship types are interpolated into Cypher, they must never come from raw input
RELATION_TYPES = tuple(THRESHOLD_MAP)


def current_ontology_hash() -> str:
    from pipeline.assemble_graph import CURRENT_ONTOLOGY_HASH  # lazy: assemble_graph imports this module
    return CURRENT_ONTOLOGY_HASH


# ---------------------------------------------------------------------------
# Pure projection logic (no I/O)
# ---------------------------------------------------------------------------

def parse_cache(cached) -> Optional[dict]:
    """Validate the JSONB payload shape; None means 'no usable cache'."""
    if isinstance(cached, str):
        try:
            cached = json.loads(cached)
        except ValueError:
            return None
    if not isinstance(cached, dict):
        return None
    if not isinstance(cached.get("entities"), list) or not isinstance(cached.get("relationships"), list):
        return None
    return cached


def _hash_projection(ontology_hash, entity_names, rels, doc_id=None) -> str:
    payload = {
        "o": ontology_hash,
        "d": doc_id,  # edges carry doc_id: adding/changing it must register as drift so the free L1 repair rewrites them
        "t": sorted(THRESHOLD_MAP.items()),
        "e": entity_names,
        "r": [[r["source"], r["type"], r["target"], r["confidence"], r["detail"]] for r in rels],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def expected_projection(cached: dict, doc_id: Optional[str] = None) -> dict:
    """What Neo4j should contain for one chunk, given its cached extraction (and the document that owns it)."""
    entities: dict = {}
    for e in cached.get("entities", []):
        name = str(e.get("name", "")).strip().upper()
        if name and name not in entities:
            entities[name] = {"name": name, "type": e.get("entity_type") or "UNSPECIFIED", "description": e.get("description") or ""}

    best: dict = {}
    for r in cached.get("relationships", []):
        rtype = str(r.get("relation_type", "")).strip().upper()
        if rtype not in THRESHOLD_MAP:
            continue
        try:
            conf = float(r.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if conf < THRESHOLD_MAP[rtype]:
            continue
        src = str(r.get("source", "")).strip().upper()
        tgt = str(r.get("target", "")).strip().upper()
        if not src or not tgt:
            continue
        key = (src, rtype, tgt)
        if key not in best or conf > best[key]["confidence"]:
            best[key] = {"source": src, "type": rtype, "target": tgt, "detail": str(r.get("detail") or ""), "confidence": round(conf, 4)}

    # An edge endpoint missing from the entity list is still an entity of this chunk (previously the edge was silently dropped)
    for src, _, tgt in best:
        for name in (src, tgt):
            entities.setdefault(name, {"name": name, "type": "UNSPECIFIED", "description": ""})

    rels = sorted(best.values(), key=lambda r: (r["source"], r["type"], r["target"]))
    ents = sorted(entities.values(), key=lambda e: e["name"])
    ontology_hash = cached.get("ontology_hash")
    return {
        "ontology_hash": ontology_hash,
        "entities": ents,
        "relationships": rels,
        "graph_hash": _hash_projection(ontology_hash, [e["name"] for e in ents], rels, doc_id),
    }


# ---------------------------------------------------------------------------
# Neo4j writes
# ---------------------------------------------------------------------------

def project_chunk(driver, chunk_id: str, doc_id: str, text: str, proj: dict) -> None:
    """Replace everything Neo4j holds for ONE chunk with its expected projection (single transaction)."""
    by_type = collections.defaultdict(list)
    for r in proj["relationships"]:
        by_type[r["type"]].append(r)

    def work(tx):
        tx.run("""
            MERGE (d:Document {id: $doc_id})
            MERGE (c:Chunk {id: $cid})
            ON CREATE SET c.created_at = timestamp()
            SET c.text = coalesce(c.text, $text), c.ontology_hash = $oh, c.graph_hash = $gh
            MERGE (d)-[:HAS_CHUNK]->(c)
        """, doc_id=doc_id, cid=chunk_id, text=text, oh=proj["ontology_hash"], gh=proj["graph_hash"])
        # Drop what this chunk contributed before (replace, never accumulate)
        tx.run("MATCH (:Entity)-[r {chunk_id: $cid}]->(:Entity) DELETE r", cid=chunk_id)
        tx.run("MATCH (:Chunk {id: $cid})-[m:MENTIONS]->(:Entity) DELETE m", cid=chunk_id)
        if proj["entities"]:
            tx.run("""
                UNWIND $ents AS e
                MERGE (n:Entity {name: e.name})
                ON CREATE SET n.type = e.type, n.description = e.description
                WITH n
                MATCH (c:Chunk {id: $cid})
                MERGE (c)-[:MENTIONS]->(n)
            """, ents=proj["entities"], cid=chunk_id)
        for rtype, rows in by_type.items():
            if rtype not in RELATION_TYPES:
                continue
            tx.run(f"""
                UNWIND $rows AS row
                MATCH (s:Entity {{name: row.source}})
                MATCH (t:Entity {{name: row.target}})
                MERGE (s)-[x:{rtype} {{chunk_id: $cid}}]->(t)
                SET x.detail = row.detail, x.confidence = row.confidence, x.ontology_hash = $oh, x.doc_id = $doc_id
            """, rows=rows, cid=chunk_id, oh=proj["ontology_hash"], doc_id=doc_id)

    with driver.session() as session:
        session.execute_write(work)


def clear_chunk(driver, chunk_id: str) -> None:
    """A chunk with no usable cache must project to nothing."""
    def work(tx):
        tx.run("MATCH (:Entity)-[r {chunk_id: $cid}]->(:Entity) DELETE r", cid=chunk_id)
        tx.run("MATCH (:Chunk {id: $cid})-[m:MENTIONS]->(:Entity) DELETE m", cid=chunk_id)
        tx.run("MATCH (c:Chunk {id: $cid}) REMOVE c.graph_hash", cid=chunk_id)
    with driver.session() as session:
        session.execute_write(work)


def refresh_entity_attributes(driver, projections_in_order: list, fill_only: bool = False) -> None:
    """Entity type/description follow the cache: the first chunk (by index) that mentions an entity wins.

    Entities are shared across documents, so a per-document repair must not overwrite what another document
    already set: with fill_only, only missing / UNSPECIFIED attributes are filled in (first document wins)."""
    attrs: dict = {}
    for proj in projections_in_order:
        for e in proj["entities"]:
            cur = attrs.get(e["name"])
            if cur is None or (cur["type"] == "UNSPECIFIED" and e["type"] != "UNSPECIFIED"):
                attrs[e["name"]] = e
    if not attrs:
        return
    if fill_only:
        cypher = """
            UNWIND $rows AS row MATCH (n:Entity {name: row.name})
            SET n.type = CASE WHEN n.type IS NULL OR n.type = 'UNSPECIFIED' THEN row.type ELSE n.type END,
                n.description = CASE WHEN coalesce(n.description, '') = '' THEN row.description ELSE n.description END
        """
    else:
        cypher = "UNWIND $rows AS row MATCH (n:Entity {name: row.name}) SET n.type = row.type, n.description = row.description"
    with driver.session() as session:
        session.run(cypher, rows=list(attrs.values()))


def purge_orphans(driver) -> tuple:
    """Delete entity edges without provenance (or whose chunk is gone) and entities no chunk mentions."""
    with driver.session() as session:
        edges = session.run("""
            MATCH (:Entity)-[r]->(:Entity)
            WHERE r.chunk_id IS NULL OR NOT EXISTS { MATCH (:Chunk {id: r.chunk_id}) }
            DELETE r
            RETURN count(*) AS n
        """).single()["n"]
        ents = session.run("""
            MATCH (e:Entity)
            WHERE NOT EXISTS { MATCH (e)<-[:MENTIONS]-(:Chunk) }
            DETACH DELETE e
            RETURN count(*) AS n
        """).single()["n"]
    return edges, ents


# ---------------------------------------------------------------------------
# Detection (no LLM tokens, one Postgres + one Neo4j round trip)
# ---------------------------------------------------------------------------

def _read_pg_cache(pg_conn, doc_id: Optional[str]) -> list[tuple]:
    """Fetch (id, document_id, extracted_graph) rows from PostgreSQL."""
    with pg_conn.cursor() as cur:
        if doc_id is None:
            cur.execute("SELECT id, document_id, extracted_graph FROM document_chunks ORDER BY chunk_index")
        else:
            cur.execute(
                "SELECT id, document_id, extracted_graph FROM document_chunks WHERE document_id = %s ORDER BY chunk_index",
                (doc_id,),
            )
        return cur.fetchall()


def _read_neo4j_state(driver, doc_id: Optional[str]) -> dict:
    """Fetch projected chunk graph_hashes, edge chunk_ids, and orphan entity counts from Neo4j."""
    with driver.session() as session:
        if doc_id is None:
            return dict(session.run("""
                CALL () { MATCH (c:Chunk) RETURN collect([c.id, c.graph_hash]) AS chunks }
                CALL () { MATCH (:Entity)-[r]->(:Entity) RETURN collect(coalesce(r.chunk_id, '')) AS edge_cids }
                CALL () { MATCH (e:Entity) WHERE NOT EXISTS { MATCH (e)<-[:MENTIONS]-(:Chunk) } RETURN count(e) AS orphan_entities }
                RETURN chunks, edge_cids, orphan_entities
            """).single())
        return dict(session.run("""
            CALL () { MATCH (:Document {id: $doc_id})-[:HAS_CHUNK]->(c:Chunk) RETURN collect([c.id, c.graph_hash]) AS chunks }
            CALL () { MATCH (:Entity)-[r]->(:Entity) WHERE r.doc_id = $doc_id RETURN collect(coalesce(r.chunk_id, '')) AS edge_cids }
            RETURN chunks, edge_cids, 0 AS orphan_entities
        """, doc_id=doc_id).single())


def _classify_chunk_drift(rows: list[tuple], neo_hash: dict, edge_counts: collections.Counter, current_hash: str) -> dict:
    """Classify each chunk row against the Neo4j projection state."""
    projections, order = {}, []
    drift, missing_cache, stale_cache, to_clear = [], [], [], []
    known_ids = set()

    for chunk_id, chunk_doc_id, cached in rows:
        known_ids.add(chunk_id)
        parsed = parse_cache(cached)
        if parsed is None:
            missing_cache.append(chunk_id)
            if neo_hash.get(chunk_id) or edge_counts.get(chunk_id):
                to_clear.append(chunk_id)  # Neo4j holds data for a chunk that has no cache
            continue
        proj = expected_projection(parsed, chunk_doc_id)
        proj["doc_id"] = chunk_doc_id
        projections[chunk_id] = proj
        order.append(chunk_id)
        if parsed.get("ontology_hash") != current_hash:
            stale_cache.append(chunk_id)  # cache is valid but made by an older ontology prompt
        if neo_hash.get(chunk_id) != proj["graph_hash"] or edge_counts.get(chunk_id, 0) != len(proj["relationships"]):
            drift.append(chunk_id)

    return {
        "projections": projections,
        "order": order,
        "drift": drift,
        "missing_cache": missing_cache,
        "stale_cache": stale_cache,
        "to_clear": to_clear,
        "known_ids": known_ids,
    }


def check_sync(pg_conn, driver, doc_id: Optional[str] = None) -> dict:
    """Compare the Neo4j projection with the Supabase cache. Read-only.

    doc_id=None checks the whole corpus (incl. global orphan entities). With a doc_id only that document's chunks
    and edges (`r.doc_id`) are compared, so the cost does not grow with the number of other documents; entities are
    shared across documents, so orphan entities are only counted by the global check (and purged by heal)."""
    t0 = time.perf_counter()
    current = current_ontology_hash()

    # Both reads are network-bound (~250 ms and ~190 ms round trips): overlap them
    with ThreadPoolExecutor(max_workers=2) as pool:
        pg_future = pool.submit(_read_pg_cache, pg_conn, doc_id)
        rec = _read_neo4j_state(driver, doc_id)
        rows = pg_future.result()

    neo_hash = {cid: gh for cid, gh in rec["chunks"]}
    edge_counts = collections.Counter(rec["edge_cids"])
    diff = _classify_chunk_drift(rows, neo_hash, edge_counts, current)

    orphan_edges = sum(n for cid, n in edge_counts.items() if cid == "" or cid not in diff["known_ids"])
    needs_extraction = diff["missing_cache"] + diff["stale_cache"]
    if diff["drift"] or diff["to_clear"] or orphan_edges or rec["orphan_entities"]:
        status = "drift"
    elif needs_extraction:
        status = "stale_cache"
    else:
        status = "in_sync"

    return {
        "status": status,
        "chunks_total": len(rows),
        "in_sync": len(rows) - len(set(diff["drift"]) | set(needs_extraction)),
        "drift": diff["drift"], "to_clear": diff["to_clear"],
        "missing_cache": diff["missing_cache"], "stale_cache": diff["stale_cache"], "needs_extraction": needs_extraction,
        "orphan_edges": orphan_edges, "orphan_entities": rec["orphan_entities"],
        "current_ontology_hash": current,
        "doc_id": doc_id,
        "checked_at": time.time(),
        "check_ms": round((time.perf_counter() - t0) * 1000, 1),
        "_projections": diff["projections"], "_order": diff["order"],
    }


def public_report(report: dict) -> dict:
    """JSON-safe view of a report (no internal projection data; long id lists are truncated)."""
    out = {k: v for k, v in report.items() if not k.startswith("_")}
    for key in ("drift", "to_clear", "missing_cache", "stale_cache", "needs_extraction"):
        out[f"{key}_count"] = len(out.get(key, []))
        out[key] = out.get(key, [])[:10]
    return out


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def heal_projection(pg_conn, driver, report: Optional[dict] = None, force_all: bool = False, doc_id: Optional[str] = None) -> dict:
    """L1: re-project drifted chunks from the cache and purge orphans. Zero LLM tokens.
    A scoped report (from check_sync(doc_id=...)) repairs only that document."""
    t0 = time.perf_counter()
    report = report or check_sync(pg_conn, driver, doc_id)
    ids = list(report["_order"]) if force_all else list(report["drift"])
    projected = 0
    if ids:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT id, document_id, text_content FROM document_chunks WHERE id = ANY(%s) ORDER BY chunk_index", (ids,))
            texts = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        def project(chunk_id):
            c_doc_id, text = texts.get(chunk_id, (None, ""))
            project_chunk(driver, chunk_id, c_doc_id, text, report["_projections"][chunk_id])
            return chunk_id

        # Chunks are independent transactions (entity names are sorted, so lock order is stable); the
        # Neo4j driver retries transient deadlocks. Network-bound, so a small pool is a large speed-up.
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
            projected = len(list(pool.map(project, ids)))
    for chunk_id in report["to_clear"]:
        clear_chunk(driver, chunk_id)
    if projected:
        refresh_entity_attributes(driver, [report["_projections"][c] for c in report["_order"]],
                                  fill_only=report.get("doc_id") is not None)
    orphan_edges, orphan_entities = purge_orphans(driver)
    return {
        "projected": projected, "cleared": len(report["to_clear"]),
        "orphan_edges_deleted": orphan_edges, "orphan_entities_deleted": orphan_entities,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def heal_extraction(pg_conn, driver, chunk_ids: Optional[list] = None, max_workers: int = 8) -> dict:
    """L2: LLM extraction for ONLY the chunks whose cache is missing or stale, then L1."""
    from google import genai
    from pipeline.assemble_graph import process_chunk_batch, BATCH_SIZE

    t0 = time.perf_counter()
    report = check_sync(pg_conn, driver)
    targets = list(chunk_ids) if chunk_ids is not None else list(report["needs_extraction"])
    extracted = failed = 0
    if targets:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY / GOOGLE_API_KEY for extraction.")
        client = genai.Client(api_key=api_key)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT id, document_id, text_content FROM document_chunks WHERE id = ANY(%s) ORDER BY chunk_index", (targets,))
            items = [(r[0], r[1], r[2]) for r in cur.fetchall()]
        batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for results in pool.map(lambda b: process_chunk_batch(client, b), batches):
                for chunk_id, _doc, _text, graph, error in results:
                    if error or graph is None:
                        failed += 1
                        print(f"  [ERROR] Extraction failed for {chunk_id}: {error}")
                        continue
                    with pg_conn.cursor() as cur:
                        cur.execute("UPDATE document_chunks SET extracted_graph = %s::jsonb WHERE id = %s", (graph.model_dump_json(), chunk_id))
                    pg_conn.commit()
                    extracted += 1
    repaired = heal_projection(pg_conn, driver)
    return {"extracted": extracted, "failed": failed, "llm_calls": (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE if targets else 0,
            "projection": repaired, "ms": round((time.perf_counter() - t0) * 1000, 1)}


# ---------------------------------------------------------------------------
# API-facing manager: TTL-cached checks, automatic L1, explicit L2
# ---------------------------------------------------------------------------

class GraphSyncManager:
    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        # Latest report per scope: key None = whole corpus, key doc_id = that document only
        self._reports: dict[Optional[str], dict] = {}
        self._last_repair: Optional[dict] = None

    @property
    def _last(self) -> Optional[dict]:
        """Latest corpus-wide report (kept for callers that predate per-document scoping)."""
        return self._reports.get(None)

    def _fresh(self, doc_id: Optional[str] = None) -> bool:
        report = self._reports.get(doc_id)
        return report is not None and (time.time() - report["checked_at"]) < self.ttl

    def invalidate(self, doc_id: Optional[str] = None) -> None:
        """Forget cached reports (the document's, and the corpus-wide one) after the data changed under us."""
        with self._lock:
            self._reports.pop(doc_id, None)
            self._reports.pop(None, None)

    def status(self, pg_conn, driver, force: bool = False, doc_id: Optional[str] = None) -> dict:
        with self._lock:
            if force or not self._fresh(doc_id):
                self._reports[doc_id] = check_sync(pg_conn, driver, doc_id)
            return public_report({**self._reports[doc_id], "last_repair": self._last_repair})

    def ensure_fresh(self, pg_conn, driver, doc_id: Optional[str] = None) -> dict:
        """Called before a query: TTL-cached check, free L1 repair if the projection drifted. Never raises.
        With a doc_id only that document is checked/repaired."""
        try:
            with self._lock:
                repaired = 0
                last = self._reports.get(doc_id)
                # Re-check when the TTL expired, or when a status call found drift that nobody has repaired yet
                # (heal_attempted stops a repair that cannot converge from being retried on every query)
                pending_drift = last is not None and last["status"] == "drift" and not last.get("heal_attempted")
                if not self._fresh(doc_id) or pending_drift:
                    report = check_sync(pg_conn, driver, doc_id)
                    if report["status"] == "drift":
                        self._last_repair = heal_projection(pg_conn, driver, report)
                        repaired = self._last_repair["projected"] + self._last_repair["cleared"] + self._last_repair["orphan_edges_deleted"]
                        report = check_sync(pg_conn, driver, doc_id)  # verify
                        report["heal_attempted"] = True
                    self._reports[doc_id] = report
                last = self._reports[doc_id]
                return {"status": last["status"], "repaired": repaired, "stale_cache": len(last["stale_cache"]) + len(last["missing_cache"]),
                        "check_ms": last["check_ms"]}
        except Exception as e:  # a sync problem must never block answering
            print(f"[WARN] graph sync check failed: {e}")
            return {"status": "unknown", "repaired": 0, "stale_cache": 0, "check_ms": 0, "error": str(e)[:160]}

    def heal(self, pg_conn, driver, mode: str) -> dict:
        with self._lock:
            if mode == "extract":
                result = heal_extraction(pg_conn, driver)
            elif mode == "project":
                result = {"projection": heal_projection(pg_conn, driver)}
            else:
                raise ValueError(f"unknown heal mode: {mode}")
            self._last_repair = result.get("projection", result)
            self._reports.clear()  # per-document reports are stale after a corpus-wide repair
            self._reports[None] = check_sync(pg_conn, driver)
            return result

    def startup(self, pg_conn, driver) -> dict:
        """Backend start: verify, repair drift for free; spend tokens only if AUTO_HEAL_EXTRACT=1."""
        info = self.ensure_fresh(pg_conn, driver)
        if self._last and self._last["needs_extraction"] and os.getenv("AUTO_HEAL_EXTRACT") == "1":
            print(f"[SYNC] AUTO_HEAL_EXTRACT=1: extracting {len(self._last['needs_extraction'])} stale chunk(s)...")
            self.heal(pg_conn, driver, "extract")
            info = {"status": self._last["status"], "repaired": 0, "stale_cache": 0, "check_ms": self._last["check_ms"]}
        return info
