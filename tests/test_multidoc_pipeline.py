"""Pure-logic tests for the multi-document pipeline (no database or network access)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.graph_sync import expected_projection
from pipeline.ingest import chunk_text, compute_doc_id, safe_filename

CACHE = {
    "ontology_hash": "abc",
    "entities": [{"name": "a", "entity_type": "X", "description": "d"}],
    "relationships": [{"source": "a", "target": "b", "relation_type": "MODULATES", "confidence": 0.9, "detail": "x"}],
}


def test_doc_id_is_sha256_of_bytes():
    assert compute_doc_id(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert compute_doc_id(b"abc") == compute_doc_id(b"abc")
    assert compute_doc_id(b"abc") != compute_doc_id(b"abd")


def test_graph_hash_covers_doc_id():
    """Adding doc_id to edges must register as drift, so the free L1 repair rewrites existing edges."""
    legacy = expected_projection(CACHE)["graph_hash"]
    assert expected_projection(CACHE, "doc1")["graph_hash"] != legacy
    assert expected_projection(CACHE, "doc1")["graph_hash"] != expected_projection(CACHE, "doc2")["graph_hash"]
    assert expected_projection(CACHE, "doc1")["graph_hash"] == expected_projection(CACHE, "doc1")["graph_hash"]


def test_chunk_text_terminates_on_unbroken_text():
    """A lone early space used to make the window step backwards forever (untrusted uploads must not hang)."""
    text = "a" * 10 + " " + "b" * 5000
    chunks = chunk_text(text)
    assert chunks and "".join(chunks).replace(" ", "").count("b") >= 5000


def test_chunk_text_basic():
    text = ("Sentence number one. " * 200).strip()
    chunks = chunk_text(text, chunk_size=1000, overlap=200)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)


def test_safe_filename():
    assert safe_filename("../../etc/pass wd.pdf") == "pass_wd.pdf"
    assert safe_filename("") == "document.pdf"
    assert safe_filename("Paper (final) v2.pdf") == "Paper_final_v2.pdf"
