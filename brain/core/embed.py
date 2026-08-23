"""
Embedding via Gemini gemini-embedding-001 (768-dim, L2-normalized).

Replaces the old fastembed / BAAI/bge-small-en-v1.5 (384-dim) path.

Public API (unchanged signatures):
  embed(text: str) -> list[float]
  embed_batch(texts: list[str]) -> list[list[float]]

Internal helper (used by brain/core/llm.py metering shim):
  embed_texts(texts: list[str], task_type: str) -> list[list[float]]
"""
import math
import os
import sys
import time

from brain.config import EMBED_BATCH_SIZE, EMBED_MODEL, EMBED_DIM
from brain.core.resilience import get_breaker, CircuitOpen

# Circuit breaker for the Gemini embed endpoint.
# Trips after 3 consecutive failures; recovers after 30 s.
_embed_breaker = get_breaker("embed", failure_threshold=3, recovery_timeout=30.0)


def _zero_vec() -> list:
    return [0.0] * EMBED_DIM


def _call_gemini(batch: list[str], task_type: str) -> list[list[float]]:
    """
    Call Gemini batchEmbedContents for one batch of texts.
    Returns L2-normalized float vectors.
    Never logs text content.
    """
    import requests as _req

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for embeddings")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta"
        f"/models/{EMBED_MODEL}:batchEmbedContents"
    )
    payload = {
        "requests": [
            {
                "model": f"models/{EMBED_MODEL}",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
                "outputDimensionality": EMBED_DIM,
            }
            for t in batch
        ]
    }

    try:
        resp = _req.post(url, json=payload, params={"key": api_key}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Gemini embed error: {exc}") from exc

    embeddings = data.get("embeddings", [])
    vectors: list[list[float]] = []
    for emb in embeddings:
        vec = emb.get("values", [])
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])

    # Defensive: if Gemini returned fewer embeddings than requested, pad with zero vecs
    while len(vectors) < len(batch):
        vectors.append(_zero_vec())

    return vectors


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """
    Embed a list of texts via Gemini gemini-embedding-001.

    task_type: "RETRIEVAL_DOCUMENT" for facts being stored,
               "RETRIEVAL_QUERY" for search queries.

    Batches in groups of EMBED_BATCH_SIZE. Returns unit-L2-normalized vectors.
    Returns [] for empty input. Never logs text content.
    """
    if not texts:
        return []

    all_vectors: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        try:
            with _embed_breaker:
                vecs = _call_gemini(batch, task_type)
            all_vectors.extend(vecs)
        except CircuitOpen as e:
            print(f"[embed] circuit open: {e}", file=sys.stderr)
            all_vectors.extend([_zero_vec() for _ in batch])
        except Exception as e:
            print(f"[embed] batch failed: {e}", file=sys.stderr)
            all_vectors.extend([_zero_vec() for _ in batch])

        # Throttle between batches to respect free-tier rate limits
        if i + EMBED_BATCH_SIZE < len(texts):
            time.sleep(0.1)

    return all_vectors


def embed(text: str) -> list:
    """
    Embed a single text for storage (RETRIEVAL_DOCUMENT task).
    Returns a zero-vector on circuit-open or unrecoverable error.
    """
    result = embed_texts([text or ""], task_type="RETRIEVAL_DOCUMENT")
    return result[0] if result else _zero_vec()


def embed_batch(texts: list) -> list:
    """
    Embed a list of texts for storage (RETRIEVAL_DOCUMENT task).
    Returns zero-vectors for any failed batch.
    """
    sanitized = [t if t and t.strip() else " " for t in texts]
    return embed_texts(sanitized, task_type="RETRIEVAL_DOCUMENT")
