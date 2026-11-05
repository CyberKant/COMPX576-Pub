import os
import re
import sys
import json
import time
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

# Resolve the root directory and append to Python path so sibling imports work directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from google import genai
from google.genai import types
from dotenv import load_dotenv
from database.database import get_postgres_connection, get_neo4j_driver
from pipeline.graph_sync import GraphSyncManager

load_dotenv(override=True)

# Architectural Constants
EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")

# Dynamically fallback to the model that worked in assemble_graph.py or current active models
SYNTHESIS_MODEL = os.getenv(
    "GEMINI_SYNTHESIS_MODEL",
    os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-2.5-flash")
)

# Query hyperparameter bounds (mirrored by QueryRequest in server_api/main.py and
# frontend/src/queryParams.js). k <= 3: worst-case path enumeration roughly doubles per hop
# (158 -> 624 -> 1224 -> 2746 paths at k=1..4 on the full corpus, measured 2026-10-07).
K_HOPS_RANGE = (1, 3)
TOP_K_RANGE = (1, 15)
TEMPERATURE_RANGE = (0.0, 1.0)
TOP_P_RANGE = (0.1, 1.0)

DEFAULT_K_HOPS = 1
DEFAULT_TOP_K_CHUNKS = 5
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9

MAX_GRAPH_EDGES = 300  # safety cap on edges returned by the k-hop traversal

# Relevance calibration: gemini-embedding-001 cosine scores live in a compressed band
# (~0.45 off-topic, ~0.70-0.80 on-topic), so raw cosine reads as "~65%" for everything.
# relevance = clamp((cos - FLOOR) / (CEIL - FLOOR), 0, 1) is query-independent and monotonic.
# Anchors measured 2026-10-06 on RETRIEVAL_DOCUMENT chunks vs RETRIEVAL_QUERY queries
# (tests/probes/embed_probe.py --post): off-topic top1 max 0.535, on-topic top1 0.641-0.779.
# Re-measure when the corpus or embedding model changes.
SIM_FLOOR = 0.54  # just above the best off-topic match
SIM_CEIL = 0.80   # slightly above the strongest on-topic match

# Strict prompt anchoring generation to the S_continuous X S_discrete intersection
SYNTHESIS_SYSTEM_PROMPT = """
You are an expert Graph-RAG synthesis engine. Synthesize answers by evaluating the intersection of two state spaces:
1. S_continuous (The raw text source chunks)
2. S_discrete (The topological causal graph edges)

OPERATIONAL CONSTRAINTS:
1. STRICT GROUNDING: Ground your statements strictly in the provided HYBRID KNOWLEDGE CONTEXT. Do not invent external benchmarks or mechanisms.
2. TOPOLOGICAL ADHERENCE: When citing structural mechanisms or dependencies, cite the exact edge format inline: (SOURCE) -[RELATION]-> (TARGET). Write every citation as plain text exactly like (MODEL_A2) -[CONSTRAINS]-> (MODEL_B2): parentheses around BOTH entity names, names copied verbatim from the context in UPPER_SNAKE_CASE, relation in UPPER_CASE, and no backticks, bold or italics around any part of it.
3. SYNTHESIS UNDER PARTIAL EVIDENCE: If the context contains relevant components (e.g., proposed architectural mechanisms and baseline evaluation metrics) but lacks an explicit direct measurement connecting them:
   - State what the graph and text establish about each component.
   - Clarify the boundary: explicitly explain that one is a proposed intervention/mechanism rather than a measured final outcome.
4. ABSOLUTE FALLBACK: Output ONLY 'INSUFFICIENT_CONTEXT: The hybrid retrieval boundary lacks the necessary causal or semantic data to resolve this query.' IF AND ONLY IF neither the entities, relations, nor relevant background metrics appear anywhere in the context.
"""

# strict_grounding=False: context first, general knowledge allowed but explicitly segregated
OPEN_SYNTHESIS_SYSTEM_PROMPT = """
You are an expert Graph-RAG synthesis engine. Synthesize answers by evaluating the intersection of two state spaces:
1. S_continuous (The raw text source chunks)
2. S_discrete (The topological causal graph edges)

OPERATIONAL CONSTRAINTS:
1. CONTEXT FIRST: Base your answer on the provided HYBRID KNOWLEDGE CONTEXT wherever it is relevant, and prefer it over prior knowledge when they conflict.
2. TOPOLOGICAL ADHERENCE: When citing structural mechanisms or dependencies from the context, cite the exact edge format inline: (SOURCE) -[RELATION]-> (TARGET). Write every citation as plain text exactly like (MODEL_A2) -[CONSTRAINS]-> (MODEL_B2): parentheses around BOTH entity names, names copied verbatim from the context in UPPER_SNAKE_CASE, relation in UPPER_CASE, and no backticks, bold or italics around any part of it.
3. GENERAL KNOWLEDGE FALLBACK: Where the context is silent or insufficient, you MAY use general knowledge, but place it ONLY under a final heading '### Beyond the retrieved context'. Never mix unsupported claims into the grounded part, and never present general knowledge as graph edges.
4. If the context contains nothing relevant, say so in one sentence, then answer under '### Beyond the retrieved context'.
"""


def _clamp(value, bounds, default, cast=float):
    """Bound a hyperparameter to [lo, hi]; non-numeric / NaN input falls back to the default."""
    lo, hi = bounds
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def build_graph(edges: list[dict]) -> dict:
    """Explicit graph structure for the client: {nodes: [{id, degree}], edges}. Empty -> both []."""
    degree: dict[str, int] = {}
    for e in edges:
        for end in (e["source"], e["target"]):
            degree[end] = degree.get(end, 0) + 1
    return {"nodes": [{"id": n, "degree": d} for n, d in degree.items()], "edges": edges}


# ---------------------------------------------------------------------------
# Question intent -> focus relation types (keyword rule, no LLM call)
# ---------------------------------------------------------------------------
# The ontology is fixed (Force Dynamics), so the *logic* of a question maps onto relation types:
# "what separates A from B" asks for DIFFERENTIATES, "what limits X" for CONSTRAINS, ...
INTENT_PATTERNS = {
    "DIFFERENTIATES": r"\b(separat\w*|differ\w*|distinguish\w*|versus|vs|compar\w*|contrast\w*|unlike|set(?:s)? apart|divergen\w*)\b",
    "CONSTRAINS": r"\b(limit\w*|constrain\w*|bottleneck\w*|prevent\w*|restrict\w*|obstacle\w*|hinder\w*|block\w*|holds? back)\b",
    "POTENTIATES": r"\b(enabl\w*|requir\w*|need\w*|depend\w*|allow\w*|prerequisite\w*|makes? possible|rel(?:y|ies) on)\b",
    "COMPOSES": r"\b(compos\w*|consist\w*|components?|parts? of|made (?:up )?of|makes? up|contain\w*|comprise\w*|includ\w*)\b",
    "ACTUALIZES": r"\b(produc\w*|generat\w*|creat\w*|yield\w*|results? in|resulted in|outputs?)\b",
    "MODULATES": r"\b(affect\w*|influenc\w*|tun(?:e|es|ed|ing)|adjust\w*|improv\w*|impact\w*|effects?|regulariz\w*|modulat\w*|chang\w*)\b",
    "MAPS_TO": r"\b(correspond\w*|maps? to|mapp\w*|represent\w*|equivalen\w*|measur\w*|relat(?:e|es|ed) to)\b",
}
_INTENT_RES = {rel: re.compile(pat, re.IGNORECASE) for rel, pat in INTENT_PATTERNS.items()}

FOCUS_DESCRIPTIONS = {
    "DIFFERENTIATES": "how entities differ or are set apart (contrast / comparison)",
    "CONSTRAINS": "what limits, bounds or blocks something",
    "POTENTIATES": "what enables, supplies or is required by something",
    "COMPOSES": "what something is made of or is part of (composition)",
    "ACTUALIZES": "what produces or brings something into existence",
    "MODULATES": "how one thing changes or tunes another",
    "MAPS_TO": "what corresponds to, represents or measures what",
}
MAX_FOCUS_RELATIONS = 2
FOCUS_EDGE_CAP = 40  # extra edges of the focus types pulled next to the retrieved entities


def detect_focus_relations(query: str) -> list[str]:
    """Relation types the question is asking about, in order of appearance (at most MAX_FOCUS_RELATIONS)."""
    hits = []
    for rel, rx in _INTENT_RES.items():
        m = rx.search(query or "")
        if m:
            hits.append((m.start(), rel))
    return [rel for _, rel in sorted(hits)[:MAX_FOCUS_RELATIONS]]


# LLM fallback for paraphrases the keyword rules cannot see ("Why can't Model C learn the small classes?").
# Runs ONLY when the rules find nothing, is cached per question, and is tiny (~300 tokens in, ~10 out).
INTENT_LLM_ENABLED = os.getenv("INTENT_LLM", "1") != "0"
INTENT_MODEL = os.getenv("GEMINI_INTENT_MODEL") or os.getenv("GEMINI_EXTRACTION_MODEL") or SYNTHESIS_MODEL
INTENT_CACHE_SIZE = 256


class _RelationName(str, Enum):
    ACTUALIZES = "ACTUALIZES"
    MODULATES = "MODULATES"
    POTENTIATES = "POTENTIATES"
    COMPOSES = "COMPOSES"
    CONSTRAINS = "CONSTRAINS"
    DIFFERENTIATES = "DIFFERENTIATES"
    MAPS_TO = "MAPS_TO"


class _IntentResult(BaseModel):
    relations: list[_RelationName] = Field(default_factory=list, description="1-2 relation types the question seeks, most important first; empty if none.")


INTENT_PROMPT = """Decide which relation types between things a question asks about (Force Dynamics ontology):
- ACTUALIZES: something produces / creates / brings another thing into existence
- MODULATES: something changes, tunes or affects another thing's state or intensity
- POTENTIATES: something enables, supplies or is required by another thing
- COMPOSES: parts form a whole; what something is made of or part of
- CONSTRAINS: something limits, bounds or blocks another thing
- DIFFERENTIATES: things differ, are contrasted, compared or set apart
- MAPS_TO: things correspond to, represent or measure one another (no causal force)
Return the 1-2 types the question primarily seeks, most important first. Return an empty list if the question is not about relations between things.

QUESTION: {query}"""


def _expand_focus_edges(session, edges: list[dict], focus_relations: list[str] | None, document_id: str) -> list[dict]:
    """Pull additional edges matching the question's focus relation types adjacent to already-retrieved entities."""
    focus = [r for r in (focus_relations or []) if r in INTENT_PATTERNS]
    if not (focus and edges):
        return edges
    names = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    extra = session.run("""
        UNWIND $names AS name
        MATCH (n:Entity {name: name})-[r]-(:Entity)
        WHERE type(r) IN $focus AND r.doc_id = $doc_id
        WITH DISTINCT r
        MATCH (source:Entity)-[r]->(target:Entity)
        WITH source, type(r) AS relation, target, head(collect(coalesce(r.detail, ''))) AS detail
        RETURN source.name AS source, relation, detail, target.name AS target
        LIMIT $cap
    """, names=names, focus=focus, doc_id=document_id, cap=FOCUS_EDGE_CAP)
    seen = {(e["source"], e["relation"], e["target"]) for e in edges}
    for rec in extra:
        key = (rec["source"], rec["relation"], rec["target"])
        if key not in seen:
            seen.add(key)
            edges.append({
                "source": rec["source"],
                "relation": rec["relation"],
                "detail": rec["detail"],
                "target": rec["target"],
            })
    edges.sort(key=lambda e: e["relation"] not in focus)  # focus relations first (stable)
    return edges


def _build_focus_prompt_prefix(focus_relations: list[str]) -> str:
    """Construct the QUESTION FOCUS directive when intent classification identifies target Force Dynamics relations."""
    if not focus_relations:
        return ""
    rels = " / ".join(focus_relations)
    return (
        f"QUESTION FOCUS: this question is about {' and '.join(FOCUS_DESCRIPTIONS[r] for r in focus_relations)}, "
        f"i.e. {rels} relations. Section [2] lists the {rels} edges first: foreground and cite them. "
        f"If no {rels} edge in the context is relevant to the question, say so explicitly instead of inventing one, "
        f"and state only what the source text supports.\n\n"
    )


class HybridQueryEngine:
    def __init__(self):
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not gemini_key:
            raise ValueError("Missing API Key.")

        self.client = genai.Client(api_key=gemini_key)
        self.pg_conn = get_postgres_connection()
        # This connection lives as long as the server and only reads. In the default transaction mode every
        # SELECT left it "idle in transaction", holding a lock on document_chunks that blocked any DDL (migrations).
        self.pg_conn.autocommit = True
        try:
            # Vector search is filtered by document_id: keep scanning the HNSW index until top_k rows survive the
            # filter (pgvector >= 0.8), otherwise a small document can get fewer results than it has chunks
            with self.pg_conn.cursor() as cur:
                cur.execute("SET hnsw.iterative_scan = strict_order")
        except Exception as e:
            print(f"[WARN] hnsw.iterative_scan unavailable (pgvector < 0.8?): {e}")
        self.neo4j_driver = get_neo4j_driver()
        # Neo4j is a projection of the Supabase extraction cache: verified (TTL-cached) before traversal,
        # drift repaired for free from the cache, LLM only for stale caches (see pipeline/graph_sync.py)
        self.sync = GraphSyncManager(ttl_seconds=float(os.getenv("GRAPH_SYNC_TTL", "60")))
        self._intent_cache: dict[str, list[str]] = {}

    def _embed_query(self, query: str) -> list[float]:
        """Project q -> v_q in R^d with strict dimensional truncation."""
        model_name = EMBEDDING_MODEL if EMBEDDING_MODEL.startswith("models/") else f"models/{EMBEDDING_MODEL}"

        response = self.client.models.embed_content(
            model=model_name,
            contents=query,
            # Asymmetric pair of RETRIEVAL_DOCUMENT chunks (pipeline/sync_embeddings.py)
            config=types.EmbedContentConfig(output_dimensionality=768, task_type="RETRIEVAL_QUERY")
        )
        return response.embeddings[0].values

    def classify_intent(self, query: str) -> tuple[list[str], str]:
        """Which relation types does the question ask about? Returns (relations, source).
        source: 'rules' (free) | 'cache' | 'llm' (one tiny call, only when the rules find nothing) | 'none'."""
        rules = detect_focus_relations(query)
        if rules:
            return rules, "rules"
        if not INTENT_LLM_ENABLED:
            return [], "none"
        key = " ".join(query.lower().split())
        if key in self._intent_cache:
            return list(self._intent_cache[key]), "cache"
        try:
            response = self.client.models.generate_content(
                model=INTENT_MODEL,
                contents=INTENT_PROMPT.format(query=query),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_IntentResult,
                    temperature=0.0,
                    max_output_tokens=64,
                ),
            )
            relations = [r.value for r in _IntentResult.model_validate_json(response.text).relations][:MAX_FOCUS_RELATIONS]
        except Exception as e:  # never block answering because the intent call failed (failures are not cached)
            print(f"[WARN] Intent classification failed, continuing without focus: {e}")
            return [], "none"
        if len(self._intent_cache) >= INTENT_CACHE_SIZE:
            self._intent_cache.pop(next(iter(self._intent_cache)))
        self._intent_cache[key] = relations
        return list(relations), "llm"

    @staticmethod
    def _calibrate(similarity: float) -> float:
        """Map raw cosine onto [0, 1] using the measured off-topic / on-topic anchors."""
        return max(0.0, min(1.0, (similarity - SIM_FLOOR) / (SIM_CEIL - SIM_FLOOR)))

    def semantic_anchoring(self, query: str, document_id: str, top_k: int = DEFAULT_TOP_K_CHUNKS) -> list[dict]:
        """Execute Vector Search bounded by cosine distance constraints, strictly within one document.
        Returns up to top_k chunks; fewer if the document holds fewer (never an error)."""
        top_k = _clamp(top_k, TOP_K_RANGE, DEFAULT_TOP_K_CHUNKS, int)
        v_q = self._embed_query(query)

        query_sql = """
            SELECT id, text_content, 1 - (embedding <=> %s::vector) AS similarity
            FROM document_chunks
            WHERE document_id = %s AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector ASC
            LIMIT %s;
        """

        with self.pg_conn.cursor() as cursor:
            cursor.execute(query_sql, (v_q, document_id, v_q, top_k))
            return [{
                "chunk_id": r[0],
                "text": r[1],
                "similarity": r[2],
                "relevance": self._calibrate(r[2])
            } for r in cursor.fetchall()]

    def bounded_graph_traversal(self, chunk_ids: list[str], k_hops: int, document_id: str,
                                focus_relations: list[str] | None = None) -> list[dict]:
        """Extract G_{local} at dynamic depth parameter k_hops via bipartite boundary projection.
        Entities are shared across documents, so scoping is done on the edges: only edges stamped with
        `doc_id = document_id` are walked or returned.
        Returns [] (never raises) when there are no seeds or Neo4j fails."""
        if not chunk_ids:
            return []
        # Cypher can't parameterize variable-length bounds, so k is cast + clamped before interpolation
        k_hops = _clamp(k_hops, K_HOPS_RANGE, DEFAULT_K_HOPS, int)
        cypher_bfs = f"""
            UNWIND $chunk_ids AS chunk_id
            MATCH (c:Chunk {{id: chunk_id}})-[:MENTIONS]->(seed:Entity)
            MATCH (seed)-[r*1..{k_hops}]-(neighbor:Entity)
            WHERE ALL(rel in r WHERE type(rel) <> 'MENTIONS' AND rel.doc_id = $doc_id)
            UNWIND r AS rel
            WITH DISTINCT rel
            MATCH (source:Entity)-[rel]->(target:Entity)
            WITH source, type(rel) AS relation, target, head(collect(coalesce(rel.detail, ''))) AS detail
            RETURN
                coalesce(source.name, 'UNKNOWN') AS source,
                relation,
                detail,
                coalesce(target.name, 'UNKNOWN') AS target
            LIMIT $max_edges
        """

        try:
            with self.neo4j_driver.session() as session:
                result = session.run(cypher_bfs, chunk_ids=chunk_ids, doc_id=document_id, max_edges=MAX_GRAPH_EDGES)
                edges = [{
                    "source": rec["source"],
                    "relation": rec["relation"],
                    "detail": rec["detail"],
                    "target": rec["target"]
                } for rec in result]
                return _expand_focus_edges(session, edges, focus_relations, document_id)
        except Exception as e:
            # Degrade to a chunk-only answer instead of failing the whole query
            print(f"[WARN] Graph traversal failed, continuing without edges: {e}")
            return []

    def assemble_gestalt_context(self, chunks: list[dict], edges: list[dict]) -> str:
        """Fuse continuous and discrete sub-states into terminal LLM context."""
        context = "================= HYBRID KNOWLEDGE CONTEXT =================\n\n"
        context += "[1] SOURCE DOCUMENTS (Semantic Anchors):\n"
        if not chunks:
            context += "No source documents retrieved.\n\n"
        for i, chunk in enumerate(chunks):
            context += f"--- Chunk {i+1} (Sim: {chunk['similarity']:.3f}) ---\n"
            context += f"{chunk['text']}\n\n"

        context += "[2] TOPOLOGICAL CAUSAL GRAPH (k-Hop Neighborhood):\n"
        if not edges:
            context += "No structural relationships found in the immediate neighborhood.\n"
        else:
            for edge in edges:
                context += f"({edge['source']}) -[{edge['relation']}]-> ({edge['target']})\n"
                context += f"   Mechanism: {edge['detail']}\n"

        context += "\n============================================================"
        return context

    def generate_synthesis(self, prompt: str, temperature: float = DEFAULT_TEMPERATURE,
                           top_p: float = DEFAULT_TOP_P, strict_grounding: bool = True) -> str:
        """f_synth: Mates the query with the gestalt context through the LLM."""
        config = types.GenerateContentConfig(
            system_instruction=SYNTHESIS_SYSTEM_PROMPT if strict_grounding else OPEN_SYNTHESIS_SYSTEM_PROMPT,
            temperature=temperature,
            top_p=top_p
        )

        try:
            response = self.client.models.generate_content(
                model=SYNTHESIS_MODEL,
                contents=prompt,
                config=config
            )
            return response.text.strip()
        except Exception as e:
            return f"SYSTEM_ERROR: Generative synthesis failed. Details: {str(e)}"

    def get_token_count(self, prompt: str) -> int:
        """Helper to measure token overhead for metric proofs."""
        try:
            response = self.client.models.count_tokens(
                model=SYNTHESIS_MODEL,
                contents=prompt
            )
            return response.total_tokens
        except Exception:
            return 0  # Fail gracefully if network errors on token calculation

    def execute_hybrid_query(self, query: str, document_id: str, k_hops: int = DEFAULT_K_HOPS,
                             top_k_chunks: int = DEFAULT_TOP_K_CHUNKS,
                             temperature: float = DEFAULT_TEMPERATURE,
                             top_p: float = DEFAULT_TOP_P,
                             strict_grounding: bool = True) -> dict:
        """Orchestrate f_fuse( f_graph( f_vector(q) ) ) and return Dual-Payload with Telemetry."""
        # Defence in depth: the API validates bounds (422), direct callers get clamped values
        k_hops = _clamp(k_hops, K_HOPS_RANGE, DEFAULT_K_HOPS, int)
        top_k_chunks = _clamp(top_k_chunks, TOP_K_RANGE, DEFAULT_TOP_K_CHUNKS, int)
        temperature = _clamp(temperature, TEMPERATURE_RANGE, DEFAULT_TEMPERATURE)
        top_p = _clamp(top_p, TOP_P_RANGE, DEFAULT_TOP_P)
        strict_grounding = bool(strict_grounding)

        print(f"\n[INFO] Evaluating Query: '{query}' with k={k_hops}, top_k={top_k_chunks}, "
              f"T={temperature}, top_p={top_p}, strict={strict_grounding}")

        start_time = time.perf_counter()

        print("       -> 1. Computing v_q & querying latent space...")
        seed_chunks = self.semantic_anchoring(query, document_id, top_k_chunks)
        chunk_ids = [c["chunk_id"] for c in seed_chunks]

        print(f"       -> 2. Executing BFS traversal (k={k_hops}) from {len(chunk_ids)} seed chunks...")
        sync_info = self.sync.ensure_fresh(self.pg_conn, self.neo4j_driver, document_id)
        focus_relations, intent_source = self.classify_intent(query)
        subgraph_edges = self.bounded_graph_traversal(chunk_ids, k_hops, document_id, focus_relations)

        print(f"       -> 3. Fusing semantic chunks and {len(subgraph_edges)} topological edges...")
        context_window = self.assemble_gestalt_context(seed_chunks, subgraph_edges)
        focus_note = _build_focus_prompt_prefix(focus_relations)
        full_prompt = f"USER QUERY: {query}\n\n{focus_note}{context_window}"

        print("       -> 4. Estimating structural context density (Tokens)...")
        token_count = self.get_token_count(full_prompt)

        print(f"       -> 5. Synthesizing final response via Generative LLM ({SYNTHESIS_MODEL})...")
        answer = self.generate_synthesis(full_prompt, temperature, top_p, strict_grounding)

        end_time = time.perf_counter()
        graph = build_graph(subgraph_edges)

        return {
            "query": query,
            "answer": answer,
            "provenance": {
                "source_chunks": seed_chunks,
                "causal_graph": subgraph_edges,  # kept for backward compatibility
                "graph": graph,
                "focus_relations": focus_relations
            },
            "telemetry": {
                "latency_ms": round((end_time - start_time) * 1000, 2),
                "token_count": token_count,
                "node_count": len(graph["nodes"]),
                "edge_count": len(graph["edges"]),
                "k_hops": k_hops,
                "chunks_requested": top_k_chunks,
                "chunks_returned": len(seed_chunks),
                "temperature": temperature,
                "top_p": top_p,
                "strict_grounding": strict_grounding,
                "graph_sync": sync_info,
                "focus_relations": focus_relations,
                "intent_source": intent_source
            }
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run one hybrid query against a single ingested document.")
    parser.add_argument("--doc", help="Document id (SHA-256). Default: the most recently uploaded ready document.")
    parser.add_argument("query", nargs="?", default="What is the structural effect of continuous label mixing on macro F1 scores?")
    cli_args = parser.parse_args()

    engine = HybridQueryEngine()
    doc_id = cli_args.doc
    if doc_id is None:
        with engine.pg_conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE status = 'ready' ORDER BY upload_timestamp DESC LIMIT 1")
            row = cur.fetchone()
        if row is None:
            raise SystemExit("No ready document found. Upload one first (UI or run_pipeline.py).")
        doc_id = row[0]
    print(f"[INFO] Document: {doc_id}")

    test_query = cli_args.query
    # Execute with k=2 to verify dynamic parameterization
    final_payload = engine.execute_hybrid_query(test_query, doc_id, k_hops=2, top_k_chunks=DEFAULT_TOP_K_CHUNKS,
                                                temperature=DEFAULT_TEMPERATURE, top_p=DEFAULT_TOP_P,
                                                strict_grounding=True)

    print("\n================= DUAL-PAYLOAD API RESPONSE =================")
    print("\n[ 1. SYNTHESIZED TEXT ANSWER ]")
    print(final_payload["answer"])

    print("\n[ 2. TELEMETRY DIAGNOSTICS ]")
    print(json.dumps(final_payload["telemetry"], indent=2))
