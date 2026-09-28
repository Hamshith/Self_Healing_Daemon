"""Runbook retrieval for incident diagnosis.

ChromaDB and sentence-transformers are used when available. A small lexical
fallback keeps local tests and daemon startup usable before those optional
packages or their model cache are installed.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import config

ROOT_DIR = Path(__file__).resolve().parent
RUNBOOKS_DIR = ROOT_DIR / "runbooks"
CHROMA_DIR = ROOT_DIR / ".chroma"

_collection: Any = None
_documents: list[dict[str, str]] = []
_model: Any = None


def _chunks(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def _load_documents() -> list[dict[str, str]]:
    documents = []
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        for index, chunk in enumerate(_chunks(path.read_text(encoding="utf-8"))):
            documents.append({"id": f"{path.stem}-{index}", "text": chunk, "source": path.name})
    return documents


def _embedding_batches(documents: list[dict[str, str]], batch_size: int):
    """Yield bounded document batches instead of encoding the full corpus at once."""
    if batch_size < 1:
        raise ValueError("Embedding batch size must be at least 1")
    for start in range(0, len(documents), batch_size):
        yield documents[start:start + batch_size]


# ── Retrieval-path tracking ────────────────────────────────────────────────
# Set at index-build time so every subsequent retrieve() call logs which
# path it actually takes.  Any analysis run that used the lexical fallback
# must be clearly distinguished from one that used semantic search.
_RETRIEVAL_MODE_SEMANTIC = "semantic"
_RETRIEVAL_MODE_LEXICAL = "lexical (chromadb/sentence-transformers unavailable)"
_retrieval_mode: str = _RETRIEVAL_MODE_LEXICAL  # overwritten in build_index if ML stack is OK


def build_index() -> int:
    """
    Build or refresh the runbook index and return the chunk count.

    Logs the retrieval mode that will be used for all subsequent retrieve()
    calls so that any analysis run is clearly tagged as semantic or lexical.
    """
    started_at = time.perf_counter()
    print(f"[rag_engine] Starting runbook index build: {RUNBOOKS_DIR}", flush=True)
    global _collection, _documents, _model, _retrieval_mode
    _documents = _load_documents()
    _collection = None
    _retrieval_mode = _RETRIEVAL_MODE_LEXICAL  # default; overwritten below on success
    print(f"[rag_engine] Loaded {len(_documents)} runbook chunks", flush=True)
    try:
        print("[rag_engine] Importing chromadb...", flush=True)
        import chromadb
        print("[rag_engine] chromadb import complete", flush=True)
        print("[rag_engine] Importing sentence_transformers...", flush=True)
        from sentence_transformers import SentenceTransformer
        print("[rag_engine] sentence_transformers import complete", flush=True)

        print("[rag_engine] Loading embedding model: all-MiniLM-L6-v2", flush=True)
        _model = SentenceTransformer(
            "all-MiniLM-L6-v2",
            token=config.HF_TOKEN,
        )
        print(f"[rag_engine] Opening ChromaDB: {CHROMA_DIR}", flush=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_or_create_collection("runbooks")
        existing_count = _collection.count()
        print(f"[rag_engine] Existing ChromaDB records: {existing_count}", flush=True)
        if existing_count:
            _collection.delete(where={})
        if _documents:
            batch_size = config.EMBEDDING_BATCH_SIZE
            batch_count = (len(_documents) + batch_size - 1) // batch_size
            print(
                f"[rag_engine] Generating embeddings in {batch_count} batches "
                f"of up to {batch_size} chunks",
                flush=True,
            )
            for batch_number, batch in enumerate(
                _embedding_batches(_documents, batch_size), start=1
            ):
                print(
                    f"[rag_engine] Embedding batch {batch_number}/{batch_count} "
                    f"({len(batch)} chunks)",
                    flush=True,
                )
                embeddings = _model.encode([item["text"] for item in batch]).tolist()
                _collection.add(
                    ids=[item["id"] for item in batch],
                    documents=[item["text"] for item in batch],
                    metadatas=[{"source": item["source"]} for item in batch],
                    embeddings=embeddings,
                )
        _retrieval_mode = _RETRIEVAL_MODE_SEMANTIC
        print(
            f"[rag_engine] Vector DB ready ({_retrieval_mode}): "
            f"{_collection.count()} records "
            f"in {time.perf_counter() - started_at:.2f}s"
        )
    except (ImportError, OSError, RuntimeError) as exc:
        # chromadb or sentence-transformers not available — fall back to
        # keyword overlap.  Log prominently so results are not silently
        # attributed to semantic search.
        _retrieval_mode = _RETRIEVAL_MODE_LEXICAL
        print(
            f"[rag_engine] WARNING: Semantic index unavailable — "
            f"ALL retrieve() calls this run will use LEXICAL (keyword-overlap) "
            f"retrieval.  Reason: {exc} "
            f"({time.perf_counter() - started_at:.2f}s)"
        )
    return len(_documents)


def _lexical_retrieve(query: str, top_k: int) -> list[dict[str, str]]:
    query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))
    ranked = []
    for document in _documents:
        terms = set(re.findall(r"[a-z0-9]+", document["text"].lower()))
        score = len(query_terms & terms)
        ranked.append((score, document))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [document for score, document in ranked[:top_k] if score > 0]


def retrieve(query: str, top_k: int = 3) -> list[dict[str, str]]:
    """
    Return the most relevant runbook chunks for *query*.

    The retrieval path (semantic or lexical) is logged on every call so that
    any analysis run using the lexical fallback is unambiguously identifiable
    in logs — the two paths are NOT interchangeable for accuracy evaluation.
    """
    if not _documents:
        print("[rag_engine] No in-memory documents; building index lazily")
        build_index()

    if _collection is not None and _model is not None and _documents:
        # ── Semantic path ─────────────────────────────────────────────────
        print(
            f"[rag_engine][retrieval=SEMANTIC] Querying top {top_k} chunks "
            f"via ChromaDB + all-MiniLM-L6-v2"
        )
        embedding = _model.encode([query]).tolist()
        result = _collection.query(query_embeddings=embedding, n_results=top_k)
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        matches = [
            {"text": text, "source": metadata.get("source", "unknown")}
            for text, metadata in zip(documents, metadatas)
        ]
        print(
            f"[rag_engine][retrieval=SEMANTIC] Matched: "
            f"{[item['source'] for item in matches]}"
        )
        return matches

    # ── Lexical fallback path ─────────────────────────────────────────────
    # Log clearly so any experiment or paper that consumed these results
    # can be distinguished from one that ran with semantic search.
    print(
        f"[rag_engine][retrieval=LEXICAL] WARNING — using keyword-overlap fallback "
        f"(chromadb/sentence-transformers unavailable). "
        f"Results are NOT comparable to semantic search. "
        f"Querying top {top_k} chunks."
    )
    matches = _lexical_retrieve(query, top_k)
    print(
        f"[rag_engine][retrieval=LEXICAL] Matched: "
        f"{[item['source'] for item in matches]}"
    )
    return matches


def get_retrieval_mode() -> str:
    """Return the active retrieval mode string for logging/reporting."""
    return _retrieval_mode


def format_context(results: list[dict[str, str]]) -> str:
    """Format retrieved chunks for inclusion in an LLM prompt."""
    if not results:
        return "(no matching runbook context available)"
    return "\n\n".join(
        f"[{item.get('source', 'runbook')}]\n{item.get('text', '')}" for item in results
    )
