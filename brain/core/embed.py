from functools import lru_cache
from fastembed import TextEmbedding
from brain.config import EMBED_BATCH_SIZE


@lru_cache(maxsize=1)
def _m():
    return TextEmbedding("BAAI/bge-small-en-v1.5")


def embed(text: str) -> list:
    return next(_m().embed([text or ""])).tolist()


def embed_batch(texts: list) -> list:
    sanitized = [t if t and t.strip() else " " for t in texts]
    results = []
    for i in range(0, len(sanitized), EMBED_BATCH_SIZE):
        chunk = sanitized[i:i + EMBED_BATCH_SIZE]
        results.extend(vec.tolist() for vec in _m().embed(chunk))
    return results
