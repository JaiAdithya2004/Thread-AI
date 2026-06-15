from functools import lru_cache
from fastembed import TextEmbedding

@lru_cache(maxsize=1)
def _m():
    return TextEmbedding("BAAI/bge-small-en-v1.5")

def embed(text):
    return next(_m().embed([text or ""])).tolist()
