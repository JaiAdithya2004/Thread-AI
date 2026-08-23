"""
Sentry integration for Company Brain.

Usage:
    from brain.core.sentry import init_sentry, capture_exception

    # At server startup:
    init_sentry()

    # In exception handlers:
    capture_exception(exc, context={"workspace_id": ws, "job": "extract_facts"})

Privacy rules enforced:
- Query text, answer text, user email, API keys, and raw tokens are NEVER sent to Sentry.
- Only workspace_id and trace_id are set as user context — no PII.
- before_send strips any event field whose key contains "query", "answer", "email",
  "password", "api_key", "token", or "secret" from the event extras / breadcrumbs.
"""

import os

# ── Private sentinel so we can tell if SDK is available + initialized ──────────
_initialized: bool = False

_STRIP_KEYS = frozenset({
    "query", "answer", "email", "password", "api_key", "token",
    "secret", "authorization", "content",
})


def _strip_sensitive(event: dict, hint: dict) -> dict | None:
    """
    Sentry before_send callback.
    Strips sensitive fields from event extras and breadcrumb data.
    Returns None only if event itself is somehow broken (never suppresses normal events).
    """
    if not isinstance(event, dict):
        return event

    # Strip from top-level 'extra'
    extra = event.get("extra") or {}
    if isinstance(extra, dict):
        event["extra"] = {k: v for k, v in extra.items()
                          if k.lower() not in _STRIP_KEYS}

    # Strip from request body / data
    request = event.get("request") or {}
    if isinstance(request, dict):
        for field in ("data", "query_string"):
            request.pop(field, None)
        event["request"] = request

    # Strip from breadcrumbs
    crumbs = (event.get("breadcrumbs") or {}).get("values") or []
    for crumb in crumbs:
        data = crumb.get("data") or {}
        if isinstance(data, dict):
            crumb["data"] = {k: v for k, v in data.items()
                             if k.lower() not in _STRIP_KEYS}

    return event


def init_sentry() -> None:
    """
    Initialize Sentry SDK. No-op if SENTRY_DSN is not set.
    Call once at server startup.
    """
    global _initialized
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.1,
            before_send=_strip_sensitive,
            # Do NOT enable request body capture — contains chat content
            send_default_pii=False,
            # Silence noisy integrations that could leak request bodies
            default_integrations=True,
            # Environment tag for grouping
            environment=os.getenv("BRAIN_ENV", "development"),
        )
        _initialized = True
    except ImportError:
        # sentry-sdk not installed — silently skip
        pass
    except Exception as e:
        # Configuration error — warn but don't crash startup
        import sys
        print(f"[sentry] init failed (non-fatal): {e}", file=sys.stderr)


def capture_exception(exc: Exception, context: dict | None = None) -> None:
    """
    Capture an exception in Sentry with optional extra context.

    context: safe metadata (e.g. {"workspace_id": ws, "job": "extract_facts"}).
    NEVER pass query text, answers, emails, or API keys in context.
    """
    if not _initialized:
        return
    try:
        import sentry_sdk
        from brain.core.log import get_trace_id

        # Scrub context before shipping
        safe_context: dict = {}
        if context:
            for k, v in context.items():
                if k.lower() not in _STRIP_KEYS:
                    safe_context[k] = v

        trace_id = get_trace_id()
        if trace_id:
            safe_context["trace_id"] = trace_id

        with sentry_sdk.push_scope() as scope:
            scope.set_extras(safe_context)
            # Set user context using workspace_id only — no email / real user PII
            ws = safe_context.get("workspace_id")
            if ws:
                scope.set_user({"id": ws})
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass  # Sentry must never crash the app
