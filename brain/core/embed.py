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
import re
import sys
import time

from brain.config import EMBED_BATCH_SIZE, EMBED_MODEL, EMBED_DIM
from brain.core.resilience import get_breaker, CircuitOpen

# Circuit breaker for the Gemini embed endpoint.
# Trips after 3 consecutive failures; recovers after 30 s.
_embed_breaker = get_breaker("embed", failure_threshold=3, recovery_timeout=30.0)
_fallback_breaker = get_breaker("embed_fallback", failure_threshold=3, recovery_timeout=30.0)

# The same Gemini embedding model, served through OpenRouter. It is used only
# when Gemini itself fails (the free tier caps requests per day), so an outage or
# an exhausted quota degrades to a paid, slightly slower call instead of storing
# zero vectors that no query can ever find. The model and dimension are
# identical, so vectors from either route live in the same space.
_OPENROUTER_EMBED_MODEL = os.getenv("BRAIN_EMBED_FALLBACK_MODEL", "google/gemini-embedding-001")


def _redact(msg: str) -> str:
    """Strip API keys that libraries echo back inside request URLs."""
    return re.sub(r"(key|api_key|token)=[^&\s'\")]+", r"\1=<redacted>", str(msg))


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
        raise RuntimeError(f"Gemini embed error: {_redact(exc)}") from None

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


def _call_openrouter(batch: list[str]) -> list[list[float]]:
    """Embed one batch through OpenRouter. Returns L2-normalized vectors."""
    import requests as _req

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    try:
        resp = _req.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": _OPENROUTER_EMBED_MODEL, "input": batch, "dimensions": EMBED_DIM},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        raise RuntimeError(f"OpenRouter embed error: {_redact(exc)}") from None

    data = sorted(data, key=lambda d: d.get("index", 0))
    vectors: list[list[float]] = []
    for item in data:
        vec = item.get("embedding", [])
        if len(vec) != EMBED_DIM:
            raise RuntimeError(f"OpenRouter returned {len(vec)}-dim vectors, expected {EMBED_DIM}")
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])
    if len(vectors) != len(batch):
        raise RuntimeError(f"OpenRouter returned {len(vectors)} vectors for {len(batch)} texts")
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
            vecs = None
        except Exception as e:
            print(f"[embed] batch failed: {_redact(e)}", file=sys.stderr)
            vecs = None

        if vecs is None:
            # Gemini unavailable: try the same model through OpenRouter before
            # giving up and storing zero vectors.
            try:
                if not os.getenv("OPENROUTER_API_KEY"):
                    raise RuntimeError("no fallback configured")
                with _fallback_breaker:
                    vecs = _call_openrouter(batch)
                print(f"[embed] served {len(batch)} text(s) via OpenRouter fallback", file=sys.stderr)
            except Exception as e:
                print(f"[embed] fallback failed: {_redact(e)}", file=sys.stderr)
                vecs = [_zero_vec() for _ in batch]
            all_vectors.extend(vecs)

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
