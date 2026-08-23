import os
from dotenv import load_dotenv
load_dotenv()  # env vars already in environment take precedence over .env

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
        return "gemini/gemini-2.5-flash"
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
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

GROK_BASE_URL = "https://api.x.ai/v1"
EMBED_DIM = 768            # gemini-embedding-001 output_dimensionality
EMBED_MODEL = "gemini-embedding-001"
# NOTE: changing EMBED_MODEL requires a full re-embed + HNSW index rebuild of all facts.
# Never mix two embedding spaces — run scripts/reembed_gemini.py then scripts/rebuild_index.py.

WORTHY_MIN_LEN = 12

# default workspace + caller identity for the single-tenant MVP.
WORKSPACE_ID = os.getenv("BRAIN_WORKSPACE", "ws_local")
CALLER = os.getenv("BRAIN_USER", "you@company.com")
CALLER_PRINCIPALS = os.getenv("BRAIN_PRINCIPALS", "you@company.com,everyone").split(",")
TEAM_ID = os.getenv("BRAIN_TEAM", "engineering")

# cheap model for classify/extract tasks
EXTRACT_MODEL: str = os.getenv("BRAIN_EXTRACT_MODEL", LLM_MODEL)

# stronger model for answer synthesis
SYNTH_MODEL: str = _auto_synth_model()

# reranking
RERANK_ENABLED: bool = os.getenv("BRAIN_RERANK", "false").lower() not in ("0", "false", "no")
RERANK_MODEL: str = os.getenv("BRAIN_RERANK_MODEL", "BAAI/bge-reranker-base")

# caching
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
CACHE_TTL: int = 300

# embedding — Gemini gemini-embedding-001 (768-dim, MRL-truncated, L2-normalized)
EMBED_BATCH_SIZE: int = 64  # controls Gemini batchEmbedContents batch size

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

# Sentry error tracking — set SENTRY_DSN to enable
SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")

# Structured logging — LOG_LEVEL controls verbosity (DEBUG / INFO / WARNING / ERROR)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# retrieval: number of top facts to surface
TOP_FACTS: int = 8

# ── cost metering ─────────────────────────────────────────────────────────────
# Per-1K token prices in USD: (prompt_price_per_1k, completion_price_per_1k)
# Source: public model pricing as of 2026-Q1. Update as pricing changes.
MODEL_PRICES: dict = {
    # OpenAI
    "gpt-4o":                        (0.005, 0.015),
    "gpt-4o-mini":                   (0.00015, 0.0006),
    "gpt-4-turbo":                   (0.01, 0.03),
    "gpt-3.5-turbo":                 (0.0005, 0.0015),
    # Anthropic
    "claude-3-5-sonnet-20241022":    (0.003, 0.015),
    "claude-3-5-haiku-20241022":     (0.001, 0.005),
    "claude-haiku-4-5-20251001":     (0.001, 0.005),
    "claude-opus-5":                 (0.015, 0.075),
    # xAI Grok
    "openai/grok-3-mini":            (0.0003, 0.0005),
    "openai/grok-3":                 (0.003, 0.015),
    # Google Gemini
    "gemini/gemini-1.5-flash":       (0.000075, 0.0003),
    "gemini/gemini-1.5-pro":         (0.00125, 0.005),
    "gemini/gemini-2.5-flash":       (0.00015, 0.0006),
    "gemini/gemini-2.5-pro":         (0.00125, 0.005),
    "gemini/gemini-2.5-flash-lite":  (0.000075, 0.0003),
    # fallback — unknown model
    "default":                       (0.001, 0.002),
}

# ── credit abstraction ────────────────────────────────────────────────────────
# 1 credit = 1K synthesis-equivalent tokens (i.e. cost of ~1 synthesis call).
# Used to normalize across models and present a simple usage unit to workspace owners.
# Synthesis at gpt-4o-mini rates: $0.0006/1K completion ≈ $0.001/credit.
CREDIT_USD_COST: float = 0.001   # 1 credit costs $0.001 in managed AI spend

# Plan monthly credit allowances (also enforced in Phase 2 subscriptions table).
# These mirror the plans seeded in DB — keep in sync.
PLAN_CREDITS: dict = {
    "free":     500,     # ~500K synthesized tokens / month
    "team":     5000,    # ~5M tokens / month
    "business": 50000,   # ~50M tokens / month
}

# Platform-owner flag: set BRAIN_PLATFORM_OWNER=true in env to access /api/_platform/*
PLATFORM_OWNER: bool = os.getenv("BRAIN_PLATFORM_OWNER", "false").lower() in ("1", "true", "yes")


# ── Per-task model routing ────────────────────────────────────────────────────
# Each task has a primary model, fallback, and temperature.
# primary/fallback are litellm model strings — set to whichever provider keys are available.
# The app reads TASK_ROUTES at startup; set BRAIN_TASK_ROUTES_JSON to override all.

def _task_routes() -> dict:
    """Build per-task routing based on which API keys are present."""
    gemini  = bool(os.getenv("GEMINI_API_KEY"))
    openai  = bool(os.getenv("OPENAI_API_KEY"))
    anthro  = bool(os.getenv("ANTHROPIC_API_KEY"))
    grok    = bool(os.getenv("GROK_API_KEY"))

    # Primary: prefer Gemini (free); fallback: OpenAI if available, else same model
    def _pair(gem_model, oai_model, anthro_model):
        if gemini:
            primary = f"gemini/{gem_model}"
            fallback = f"openai/{oai_model}" if openai else (f"anthropic/{anthro_model}" if anthro else primary)
        elif openai:
            primary = f"openai/{oai_model}"
            fallback = f"gemini/{gem_model}" if gemini else primary
        elif anthro:
            primary = f"anthropic/{anthro_model}"
            fallback = primary
        elif grok:
            primary = "openai/grok-3-mini"
            fallback = primary
        else:
            primary = "gpt-4o-mini"   # mock mode
            fallback = primary
        return primary, fallback

    extract_p, extract_f = _pair("gemini-2.5-flash", "gpt-4o-mini", "claude-haiku-4-5-20251001")
    synth_p,   synth_f   = _pair("gemini-2.5-flash", "gpt-4o",      "claude-3-5-sonnet-20241022")

    return {
        "extract": {
            "primary":     extract_p,
            "fallback":    extract_f,
            "temperature": 0.1,    # low temp for structured extraction
        },
        "synthesize": {
            "primary":     synth_p,
            "fallback":    synth_f,
            "temperature": 0.3,
            "max_tokens":  400,    # answers are 1-3 sentences; cap to reduce latency
        },
        "classify": {
            "primary":     extract_p,
            "fallback":    extract_f,
            "temperature": 0.0,
        },
        "chat": {
            "primary":     synth_p,
            "fallback":    synth_f,
            "temperature": 0.4,
            "max_tokens":  600,
        },
    }

TASK_ROUTES: dict = _task_routes()


# ── Boot validation ───────────────────────────────────────────────────────────

def startup_check() -> None:
    """Log configuration summary and abort if required production keys are missing.

    Called once at server startup from brain.wrappers.api.main().
    Prints a clear summary so operators know exactly what is and isn't configured.
    In production (BRAIN_ENV=production) missing required keys cause sys.exit(1).
    """
    import sys

    env        = os.getenv("BRAIN_ENV", "development").lower()
    is_prod    = env == "production"
    bypass     = os.getenv("BRAIN_DEV_BYPASS", "").lower() in ("1", "true", "yes")

    ok  = "\033[32mOK\033[0m"
    warn = "\033[33m--\033[0m"
    err  = "\033[31mERR\033[0m"

    checks = []
    fatal  = []

    def _check(label: str, value: str, required_in_prod: bool = False) -> None:
        present = bool(value)
        if present:
            checks.append(f"  {ok}  {label}")
        elif required_in_prod and is_prod:
            checks.append(f"  {err}  {label}  ← REQUIRED in production")
            fatal.append(label)
        else:
            checks.append(f"  {warn}  {label}  (not set)")

    print("\n[thread] -- Boot check -----------------------------------")
    print(f"  env        : {env}")
    _check("DATABASE_URL",     os.getenv("DATABASE_URL", ""),     required_in_prod=True)
    if is_prod:
        _check("CLERK_SECRET_KEY", os.getenv("CLERK_SECRET_KEY", ""), required_in_prod=True)
    _check("LLM key (any)",
           next((os.getenv(k, "") for k in
                 ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROK_API_KEY", "GEMINI_API_KEY")
                 if os.getenv(k)), ""),
           required_in_prod=True)
    _check("SLACK_BOT_TOKEN",   os.getenv("SLACK_BOT_TOKEN", ""))
    _check("COMPOSIO_API_KEY",  os.getenv("COMPOSIO_API_KEY", ""))
    _check("LANGFUSE (tracing)", os.getenv("LANGFUSE_PUBLIC_KEY", ""))
    for line in checks:
        print(line)

    if bypass and is_prod:
        print(f"\n  {err}  FATAL: BRAIN_DEV_BYPASS=true with BRAIN_ENV=production — refusing to start.")
        sys.exit(1)

    if fatal:
        print(f"\n  {err}  FATAL: missing required keys in production: {', '.join(fatal)}")
        print("         Set them in your environment or .env file and restart.")
        sys.exit(1)

    print("[thread] -- Boot check passed -----------------------------------\n")
