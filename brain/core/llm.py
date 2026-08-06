import json, os, sys
from brain.config import LLM_MODEL, LANGFUSE_ENABLED

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
        print(f"[Intellark] Langfuse init failed (tracing disabled): {_e}", file=sys.stderr)
        _langfuse = None


def _grok_kwargs():
    grok_key = os.getenv("GROK_API_KEY")
    if grok_key:
        from brain.config import GROK_BASE_URL
        return {"api_key": grok_key, "api_base": GROK_BASE_URL}
    return {}


def _has_key():
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY") or os.getenv("GROK_API_KEY"))


def _build_kwargs(system: str, user: str, model: str = None, json_mode: bool = True) -> dict:
    kwargs = dict(
        model=model or LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.1,
        **_grok_kwargs(),
    )
    # BYO-key override: inject the workspace's own API key for this request
    byo_provider, byo_key = _effective_api_key()
    if byo_key:
        if byo_provider == "openai":
            kwargs["api_key"] = byo_key
        elif byo_provider == "anthropic":
            kwargs["api_key"] = byo_key
        elif byo_provider == "grok":
            from brain.config import GROK_BASE_URL
            kwargs["api_key"]  = byo_key
            kwargs["api_base"] = GROK_BASE_URL
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def _trace_start(task_name: str, model: str, messages: list):
    if _langfuse is None:
        return None
    try:
        trace = _langfuse.trace(name=task_name)
        return trace.generation(name=task_name, model=model, input=messages)
    except Exception as e:
        print(f"[Intellark] Langfuse span failed: {e}", file=sys.stderr)
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
        print(f"[Intellark] Langfuse end failed: {e}", file=sys.stderr)


# ── cost metering & BYO-key ───────────────────────────────────────────────────

# Thread-local context set by API layer so llm.py knows who to bill
_ctx: dict = {}

def set_billing_context(account_id: str, user_id: str, team_id: str = None):
    """Call once per request (before any LLM calls) to attach billing context."""
    _ctx["account_id"] = account_id
    _ctx["user_id"]    = user_id
    _ctx["team_id"]    = team_id
    # Eagerly load BYO key so all calls in this request use it
    _ctx.pop("byo_provider", None)
    _ctx.pop("byo_key", None)
    if account_id:
        _load_byo_key(account_id)


def clear_billing_context():
    _ctx.clear()


def _load_byo_key(account_id: str):
    """Read workspace BYO key from DB and stash it in _ctx for this request."""
    try:
        from brain.core.db import BACKEND
        if BACKEND != "postgres":
            return
        from brain.core.db_pg import get_byo_key
        for provider in ("openai", "anthropic", "grok"):
            key = get_byo_key(account_id, provider)
            if key:
                _ctx["byo_provider"] = provider
                _ctx["byo_key"]      = key
                return
    except Exception as e:
        print(f"[Intellark] BYO-key load failed: {e}", file=sys.stderr)


def _effective_api_key() -> tuple:
    """Return (provider, api_key) to use for this request — BYO wins over env."""
    if _ctx.get("byo_key"):
        return _ctx["byo_provider"], _ctx["byo_key"]
    return None, None


def _check_credit_allowance():
    """Raise HTTP 402 if this account has exhausted its monthly credit allowance.
    Silently passes in dev/bypass mode (no account_id) or non-postgres backends."""
    account_id = _ctx.get("account_id")
    if not account_id:
        return
    # BYO-key accounts are never credit-capped — they pay their own provider directly
    if _ctx.get("byo_key"):
        return
    try:
        from brain.core.db import BACKEND
        if BACKEND != "postgres":
            return
        from brain.core.db_pg import get_plan_for_account, get_credits_used_this_month
        plan      = get_plan_for_account(account_id)
        allowance = plan.get("monthly_credits", 500)
        used      = get_credits_used_this_month(account_id)
        if used >= allowance:
            # Import here to avoid circular import at module load time
            from fastapi import HTTPException
            raise HTTPException(
                status_code=402,
                detail=f"Monthly credit limit reached ({used}/{allowance} credits used). "
                       "Upgrade your plan or add your own API key in Settings."
            )
    except Exception as e:
        # Only re-raise HTTP 402; swallow all other errors so metering never breaks queries
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            raise
        print(f"[Intellark] credit check failed (non-fatal): {e}", file=sys.stderr)


def _write_usage(task: str, model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float):
    """Write one row to usage_events in Postgres. Silently skips on any error."""
    account_id = _ctx.get("account_id")
    user_id    = _ctx.get("user_id", "unknown")
    if not account_id:
        return  # no billing context — dev/bypass mode, skip silently
    try:
        from brain.core.db import BACKEND
        if BACKEND != "postgres":
            return
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
        print(f"[Intellark] usage_events write failed: {e}", file=sys.stderr)


def _extract_usage(resp, model: str, task: str):
    """Pull token counts from a litellm response and write to usage_events."""
    try:
        usage = resp.usage
        if usage is None:
            return
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        from brain.config import MODEL_PRICES
        prices = MODEL_PRICES.get(model, MODEL_PRICES.get("default", (0.0, 0.0)))
        cost = (pt * prices[0] + ct * prices[1]) / 1000.0
        _write_usage(task, model, pt, ct, cost)
    except Exception as e:
        print(f"[Intellark] usage extraction failed: {e}", file=sys.stderr)


# ── LLM call helpers ──────────────────────────────────────────────────────────

def chat_json(system: str, user: str, model: str = None, task_name: str = "unknown") -> dict:
    _check_credit_allowance()
    if not _has_key() and not _ctx.get("byo_key"):
        print("[Intellark] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return {"facts": [], "terms": []}

    kwargs = _build_kwargs(system, user, model=model, json_mode=True)
    gen = _trace_start(task_name, kwargs["model"], kwargs["messages"])

    try:
        resp = _completion(**kwargs)
        text = resp.choices[0].message.content
        _extract_usage(resp, kwargs["model"], task_name)
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            i = min((text.find("["), text.find("{")), key=lambda x: (x < 0, x))
            result = json.loads(text[i:])
        _trace_end(gen, result)
        return result
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[Intellark] LLM call failed: {e}", file=sys.stderr)
        return {"facts": [], "terms": []}


def chat_text(system: str, user: str, model: str = None, task_name: str = "unknown") -> str:
    _check_credit_allowance()
    if not _has_key() and not _ctx.get("byo_key"):
        print("[Intellark] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return ""

    kwargs = _build_kwargs(system, user, model=model, json_mode=False)
    gen = _trace_start(task_name, kwargs["model"], kwargs["messages"])

    try:
        resp = _completion(**kwargs)
        result = resp.choices[0].message.content or ""
        _extract_usage(resp, kwargs["model"], task_name)
        _trace_end(gen, result)
        return result
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[Intellark] LLM call failed: {e}", file=sys.stderr)
        return ""


def validate_llm_output(data: dict, model_class: type) -> tuple:
    """Validate data against a Pydantic BaseModel. Returns (ok, instance, error_str)."""
    try:
        instance = model_class(**data)
        return (True, instance, "")
    except Exception as e:
        error_str = str(e)
        print(f"[Intellark] LLM output validation failed ({model_class.__name__}): {error_str}",
              file=sys.stderr)
        return (False, None, error_str)
