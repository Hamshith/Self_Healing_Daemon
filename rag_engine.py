"""Runbook retrieval for incident diagnosis.

ChromaDB and sentence-transformers are used when available. A small lexical
fallback keeps local tests and daemon startup usable before those optional
packages or their model cache are installed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


def build_index() -> int:
    """Build or refresh the runbook index and return the chunk count."""
    global _collection, _documents, _model
    _documents = _load_documents()
    _collection = None
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_or_create_collection("runbooks")
        if _collection.count():
            _collection.delete(where={})
        if _documents:
            embeddings = _model.encode([item["text"] for item in _documents]).tolist()
            _collection.add(
                ids=[item["id"] for item in _documents],
                documents=[item["text"] for item in _documents],
                metadatas=[{"source": item["source"]} for item in _documents],
                embeddings=embeddings,
            )
    except (ImportError, OSError, RuntimeError) as exc:
        # Retrieval still works lexically when optional ML dependencies are absent.
        print(f"[rag_engine] Semantic index unavailable; using lexical retrieval: {exc}")
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
    """Return the most relevant runbook chunks for *query*."""
    if not _documents:
        build_index()
    if _collection is not None and _model is not None and _documents:
        embedding = _model.encode([query]).tolist()
        result = _collection.query(query_embeddings=embedding, n_results=top_k)
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        return [
            {"text": text, "source": metadata.get("source", "unknown")}
            for text, metadata in zip(documents, metadatas)
        ]
    return _lexical_retrieve(query, top_k)


def format_context(results: list[dict[str, str]]) -> str:
    """Format retrieved chunks for inclusion in an LLM prompt."""
    if not results:
        return "(no matching runbook context available)"
    return "\n\n".join(
        f"[{item.get('source', 'runbook')}]\n{item.get('text', '')}" for item in results
    )
