import json, os, sys
from litellm import completion
from brain.config import LLM_MODEL, LANGFUSE_ENABLED

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
        print(f"[Company Brain] Langfuse init failed (tracing disabled): {_e}", file=sys.stderr)
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
        print(f"[Company Brain] Langfuse span failed: {e}", file=sys.stderr)
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
        print(f"[Company Brain] Langfuse end failed: {e}", file=sys.stderr)


def chat_json(system: str, user: str, model: str = None, task_name: str = "unknown") -> dict:
    if not _has_key():
        print("[Company Brain] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return {"facts": [], "terms": []}

    kwargs = _build_kwargs(system, user, model=model, json_mode=True)
    gen = _trace_start(task_name, kwargs["model"], kwargs["messages"])

    try:
        resp = completion(**kwargs)
        text = resp.choices[0].message.content
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            i = min((text.find("["), text.find("{")), key=lambda x: (x < 0, x))
            result = json.loads(text[i:])
        _trace_end(gen, result)
        return result
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[Company Brain] LLM call failed: {e}", file=sys.stderr)
        return {"facts": [], "terms": []}


def chat_text(system: str, user: str, model: str = None, task_name: str = "unknown") -> str:
    if not _has_key():
        print("[Company Brain] No API key detected — set an LLM key in .env.", file=sys.stderr)
        return ""

    kwargs = _build_kwargs(system, user, model=model, json_mode=False)
    gen = _trace_start(task_name, kwargs["model"], kwargs["messages"])

    try:
        resp = completion(**kwargs)
        result = resp.choices[0].message.content or ""
        _trace_end(gen, result)
        return result
    except Exception as e:
        _trace_end(gen, e, error=True)
        print(f"[Company Brain] LLM call failed: {e}", file=sys.stderr)
        return ""


def validate_llm_output(data: dict, model_class: type) -> tuple:
    """Validate data against a Pydantic BaseModel. Returns (ok, instance, error_str)."""
    try:
        instance = model_class(**data)
        return (True, instance, "")
    except Exception as e:
        error_str = str(e)
        print(f"[Company Brain] LLM output validation failed ({model_class.__name__}): {error_str}",
              file=sys.stderr)
        return (False, None, error_str)
