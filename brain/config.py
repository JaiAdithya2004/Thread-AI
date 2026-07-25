import os
from dotenv import load_dotenv
load_dotenv(override=True)

def _auto_model():
    explicit = os.getenv("BRAIN_LLM_MODEL")
    if explicit:
        return explicit
    if os.getenv("GROK_API_KEY"):
        return "openai/grok-3-mini"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude-haiku-4-5-20251001"
    if os.getenv("OPENAI_API_KEY"):
        return "gpt-4o-mini"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini/gemini-1.5-flash"
    return "gpt-4o-mini"   # mock mode fallback label

LLM_MODEL = _auto_model()

def _auto_synth_model():
    explicit = os.getenv("BRAIN_SYNTH_MODEL")
    if explicit:
        return explicit
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude-3-5-sonnet-20241022"
    if os.getenv("OPENAI_API_KEY"):
        return "gpt-4o"
    return LLM_MODEL

# Supabase Auth — set both to enable JWT verification in production.
# Find them in: Supabase dashboard → Settings → API
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

GROK_BASE_URL = "https://api.x.ai/v1"
EMBED_DIM = 384                      # bge-small
DB_FILENAME = os.getenv("BRAIN_DB", "brain.db")

# memory-worthiness: drop noise (promo/social/chitchat) before extraction
WORTHY_MIN_LEN = 12

# default workspace + caller identity for the single-tenant MVP.
# In the hosted version these come from auth, not env.
WORKSPACE_ID = os.getenv("BRAIN_WORKSPACE", "ws_local")
CALLER = os.getenv("BRAIN_USER", "you@company.com")
# principals the caller belongs to (used for ACL filtering)
CALLER_PRINCIPALS = os.getenv("BRAIN_PRINCIPALS", "you@company.com,everyone").split(",")
# which team the caller belongs to (used for team-scoped retrieval)
TEAM_ID = os.getenv("BRAIN_TEAM", "engineering")

# cheap model for classify/extract tasks
EXTRACT_MODEL: str = os.getenv("BRAIN_EXTRACT_MODEL", LLM_MODEL)

# stronger model for answer synthesis
SYNTH_MODEL: str = _auto_synth_model()

# reranking
RERANK_ENABLED: bool = os.getenv("BRAIN_RERANK", "true").lower() not in ("0", "false", "no")
RERANK_MODEL: str = os.getenv("BRAIN_RERANK_MODEL", "BAAI/bge-reranker-base")

# caching
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
CACHE_TTL: int = 300  # seconds

# embedding
EMBED_BATCH_SIZE: int = 64

# deduplication: cosine similarity threshold for entity dedup
DEDUP_THRESHOLD: float = 0.92

# confidence: below this threshold → "not sure" path
CONFIDENCE_THRESHOLD: float = 0.35

# observability
LANGFUSE_ENABLED: bool = bool(
    os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
)
LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

# retrieval: number of top facts to surface
TOP_FACTS: int = 12
