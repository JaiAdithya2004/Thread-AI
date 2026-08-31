import json
import os
import sys
from brain.config import LANGFUSE_ENABLED
from brain.core.resilience import retry, get_breaker, CircuitOpen
import brain.config as config

# LLM call timeout in seconds
_LLM_TIMEOUT = int(os.getenv("BRAIN_LLM_TIMEOUT", "30"))

def _completion(**kwargs):
    from litellm import completion
    return completion(**kwargs)

# Langfuse singleton — created once if credentials are set
_langfuse = None
if LANGFUSE_ENABLED:
    try:
        from langfuse import Langfuse
        from brain.config import LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_HOST
        _langfuse = Langfuse(
            secret_key=LANGFUSE_SECRET_KEY,
            public_key=LANGFUSE_PUBLIC_KEY,
            host=LANGFUSE_HOST,
        )
    except Exception as _e:
        print(f"[thread] Langfuse init failed (tracing disabled): {_e}", file=sys.stderr)
        _langfuse = None


def _provider_kwargs(model: str) -> dict:
    """Return provider-specific kwargs for litellm based on the model prefix."""
    grok_key = os.getenv("GROK_API_KEY")
    or_key = os.getenv("OPENROUTER_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if model.startswith("openrouter/") and or_key:
        return {"api_key": or_key, "api_base": "https://openrouter.ai/api/v1"}
    if grok_key and model.startswith("openai/grok"):
        from brain.config import GROK_BASE_URL
        return {"api_key": grok_key, "api_base": GROK_BASE_URL}
    if model.startswith("gemini/") and gemini_key:
        return {"api_key": gemini_key}
    return {}


def _grok_kwargs():
    """Legacy shim — kept so _build_kwargs still works; returns empty (provider_kwargs now handles it)."""
    return {}


def _has_key():
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY") or os.getenv("GROK_API_KEY")
                or os.getenv("OPENROUTER_API_KEY") or os.getenv("MOONSHOT_API_KEY"))


def _model_supports_json_mode(model: str) -> bool:
    """Some OpenRouter free models don't support response_format:json_object."""
    if "llama" in model or "mistral" in model:
        return False
    return True


def _build_kwargs(system: str, user: str, model: str, temperature: float,
                  json_mode: bool = True, max_tokens: int = None) -> dict:
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=temperature,
        timeout=_LLM_TIMEOUT,
        **_provider_kwargs(model),
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode and _model_supports_json_mode(model):
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def _trace_start(task_name: str, model: str, messages: list):
    if _langfuse is None:
        return None
    try:
        trace = _langfuse.trace(name=task_name)
        return trace.generation(name=task_name, model=model, input=messages)
    except Exception as e:
        print(f"[thread] Langfuse span failed: {e}", file=sys.stderr)
        return None


def _trace_end(gen, output, error=False):
    if gen is None:
        return
    try:
        if error:
            gen.end(output={"error": str(output)}, level="ERROR")
        else:
            gen.end(output=output)
    except Exception as e:
        print(f"[thread] Langfuse end failed: {e}", file=sys.stderr)


# ── cost metering ─────────────────────────────────────────────────────────────

_ctx: dict = {}

def _load_byo_key(account_id: str):
    """Load the BYO API key for this account and stash it in _ctx."""
    try:
        # One query for all providers: looping get_byo_key would take a separate
        # pooled connection per provider while the request already holds one.
        from brain.core.db_pg import get_byo_keys
        providers = ("openai", "anthropic", "gemini")
        found = get_byo_keys(account_id, providers)
        for provider in providers:          # preserve preference order
            if found.get(provider):
                _ctx["byo_provider"] = provider
                _ctx["byo_key"]      = found[provider]
                return
    except Exception as e:
        print(f"[thread] BYO key load failed (non-fatal): {e}", file=sys.stderr)


def _effective_api_key() -> tuple:
    """Return (provider, key) for the active BYO key, or (None, None)."""
    provider = _ctx.get("byo_provider")
    key      = _ctx.get("byo_key")
    if provider and key:
        return provider, key
    return None, None


def set_billing_context(account_id: str, user_id: str, team_id: str = None):
    _ctx["account_id"] = account_id
    _ctx["user_id"]    = user_id
    _ctx["team_id"]    = team_id
    _load_byo_key(account_id)


def clear_billing_context():
    _ctx.clear()


def _check_credit_allowance():
    account_id = _ctx.get("account_id")
    if not account_id:
        return
    # BYO-key accounts are never credit-capped
    if _ctx.get("byo_key"):
        return
    try:
        from brain.core.db_pg import get_plan_for_account, get_credits_used_this_month
        plan      = get_plan_for_account(account_id)
        allowance = plan.get("monthly_credits", 500)
        used      = get_credits_used_this_month(account_id)
        if used >= allowance:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=402,
                detail=f"Monthly credit limit reached ({used}/{allowance} credits used). "
                       "Upgrade your plan to continue using AI features."
            )
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            raise
        print(f"[thread] credit check failed (non-fatal): {e}", file=sys.stderr)


def _write_usage(task: str, model: str, prompt_tokens: int,
                 completion_tokens: int, cost_usd: float):
    account_id = _ctx.get("account_id")
    user_id    = _ctx.get("user_id", "unknown")
    if not account_id:
        return
    try:
        from brain.core import db_pg as _pg
        conn = _pg.connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO usage_events
                    (account_id, user_id, team_id, task, model,
                     prompt_tokens, completion_tokens, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (account_id, user_id, _ctx.get("team_id"), task, model,
                  prompt_tokens, completion_tokens, cost_usd))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print(f"[thread] usage_events write failed: {e}", file=sys.stderr)


def _extract_usage(resp, model: str, task: str):
    try:
        usage = resp.usage
        if usage is None:
            return
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        prices = config.MODEL_PRICES.get(model, config.MODEL_PRICES.get("default", (0.0, 0.0)))
        cost = (pt * prices[0] + ct * prices[1]) / 1000.0
        _write_usage(task, model, pt, ct, cost)
    except Exception as e:
        print(f"[thread] usage extraction failed: {e}", file=sys.stderr)


# ── LLM call helpers ──────────────────────────────────────────────────────────

# Circuit breaker for LLM provider — trips after 5 consecutive failures,
# recovers after 30 s.  All chat_json / chat_text calls share this breaker.
_llm_breaker = get_breaker("llm", failure_threshold=5, recovery_timeout=30.0)


@retry(max_attempts=3, base_delay=1.0, max_delay=8.0, jitter=0.4)
def _call_llm(**kwargs):
    """Single litellm call with retry wrapping.  Circuit breaker is applied by callers."""
    return _completion(**kwargs)


def _call_llm_with_fallback(primary: str, fallback: str, temperature: float,
                             **kwargs) -> tuple:
    """Try primary model; on circuit-open or exhausted retries, try fallback once."""
    try:
        with _llm_breaker:
            resp = _call_llm(model=primary, temperature=temperature, **kwargs)
        return resp, primary
    except (CircuitOpen, Exception) as e:
        if primary == fallback:
            raise
        print(f"[llm] primary {primary} failed ({e!r}), trying fallback {fallback}",
              file=sys.stderr)
        # Re-inject provider kwargs for the fallback model (different provider = different creds)
        fallback_kwargs = {k: v for k, v in kwargs.items()
                          if k not in ("api_key", "api_base")}
        fallback_kwargs.update(_provider_kwargs(fallback))
        resp = _call_llm.__wrapped__(model=fallback, temperature=temperature, **fallback_kwargs)
        return resp, fallback


def chat_json(system: str, user: str, model: str = None,
              task_name: str = "extract") -> dict:
    _check_credit_allowance()
    if not _has_key():
        print("[thread] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return {"facts": [], "terms": []}

    # Resolve model + temperature from TASK_ROUTES unless caller passed model explicitly
    if model is not None:
        primary_model  = model
        fallback_model = model
        temperature    = 0.1
        max_tokens     = None
    else:
        route          = config.TASK_ROUTES.get(task_name, config.TASK_ROUTES["extract"])
        primary_model  = route["primary"]
        fallback_model = route["fallback"]
        temperature    = route.get("temperature", 0.1)
        max_tokens     = route.get("max_tokens", None)

    kwargs = _build_kwargs(system, user, model=primary_model, temperature=temperature,
                           json_mode=True, max_tokens=max_tokens)
    # Remove model from kwargs — _call_llm_with_fallback passes it explicitly
    base_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "temperature")}
    gen = _trace_start(task_name, primary_model, kwargs["messages"])

    try:
        resp, model_used = _call_llm_with_fallback(
            primary_model, fallback_model, temperature, **base_kwargs)
        print(f"[llm] task={task_name} model={model_used}", file=sys.stderr)
        text = resp.choices[0].message.content
        _extract_usage(resp, model_used, task_name)
        # Strip markdown code fences that free models often wrap JSON in
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0].strip()
        try:
            result = json.loads(clean)
        except json.JSONDecodeError:
            i = min((clean.find("["), clean.find("{")), key=lambda x: (x < 0, x))
            result = json.loads(clean[i:])
        _trace_end(gen, result)
        return result
    except CircuitOpen as e:
        print(f"[thread] LLM circuit open — returning empty: {e}", file=sys.stderr)
        _trace_end(gen, str(e), error=True)
        return {"facts": [], "terms": []}
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[thread] LLM call failed: {e}", file=sys.stderr)
        return {"facts": [], "terms": []}


def chat_text(system: str, user: str, model: str = None,
              task_name: str = "synthesize") -> str:
    _check_credit_allowance()
    if not _has_key():
        print("[thread] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return ""

    # Resolve model + temperature from TASK_ROUTES unless caller passed model explicitly
    if model is not None:
        primary_model  = model
        fallback_model = model
        temperature    = 0.3
        max_tokens     = None
    else:
        route          = config.TASK_ROUTES.get(task_name, config.TASK_ROUTES["synthesize"])
        primary_model  = route["primary"]
        fallback_model = route["fallback"]
        temperature    = route.get("temperature", 0.3)
        max_tokens     = route.get("max_tokens", None)

    kwargs = _build_kwargs(system, user, model=primary_model, temperature=temperature,
                           json_mode=False, max_tokens=max_tokens)
    base_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "temperature")}
    gen = _trace_start(task_name, primary_model, kwargs["messages"])

    try:
        resp, model_used = _call_llm_with_fallback(
            primary_model, fallback_model, temperature, **base_kwargs)
        print(f"[llm] task={task_name} model={model_used}", file=sys.stderr)
        result = resp.choices[0].message.content or ""
        _extract_usage(resp, model_used, task_name)
        _trace_end(gen, result)
        return result
    except CircuitOpen as e:
        print(f"[thread] LLM circuit open — returning empty: {e}", file=sys.stderr)
        _trace_end(gen, str(e), error=True)
        return ""
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[thread] LLM call failed: {e}", file=sys.stderr)
        return ""


def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """
    Embed texts via gemini-embedding-001 (768-dim, L2-normalized).

    task_type: "RETRIEVAL_DOCUMENT" for facts being stored,
               "RETRIEVAL_QUERY" for search queries.

    Batches texts in groups of EMBED_BATCH_SIZE to respect free-tier limits.
    Each returned vector is unit-L2-normalized (required for MRL-truncated dims).
    Meters token usage into usage_events with task='embed'.

    Never logs the text content.
    """
    if not texts:
        return []

    from brain.core.embed import embed_texts as _embed_texts

    all_vectors = _embed_texts(texts, task_type=task_type)

    # Approximate token count for metering (Gemini embed doesn't return token counts)
    total_chars = sum(len(t) for t in texts)
    total_tokens = max(1, total_chars // 4)

    try:
        _write_usage(
            task="embed",
            model=config.EMBED_MODEL,
            prompt_tokens=total_tokens,
            completion_tokens=0,
            cost_usd=0.0,  # Gemini embedding is free-tier; update if billed
        )
    except Exception:
        pass  # metering failure must not break embedding

    return all_vectors


def validate_llm_output(data: dict, model_class: type) -> tuple:
    """Validate data against a Pydantic BaseModel. Returns (ok, instance, error_str)."""
    try:
        instance = model_class(**data)
        return (True, instance, "")
    except Exception as e:
        error_str = str(e)
        print(f"[thread] LLM output validation failed ({model_class.__name__}): {error_str}",
              file=sys.stderr)
        return (False, None, error_str)
