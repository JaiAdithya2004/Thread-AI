import os
from dotenv import load_dotenv
load_dotenv()

def _auto_model():
    explicit = os.getenv("BRAIN_LLM_MODEL")
    if explicit:
        return explicit
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude-haiku-4-5-20251001"
    if os.getenv("OPENAI_API_KEY"):
        return "gpt-4o-mini"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini/gemini-1.5-flash"
    return "gpt-4o-mini"   # mock mode fallback label

LLM_MODEL = _auto_model()
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
