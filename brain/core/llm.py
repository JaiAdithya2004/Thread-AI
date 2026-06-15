import json, os, sys
from litellm import completion
from brain.config import LLM_MODEL

def get_mock_response(system, user):
    user_lower = user.lower()
    if "related search terms" in system.lower():
        if "redis" in user_lower:
            return {"terms": ["redis", "postgres", "idempotency", "locks"]}
        elif "refund" in user_lower:
            return {"terms": ["refunds", "shipping", "date", "delay"]}
        return {"terms": []}

    if "dropping redis" in user_lower:
        return {
            "facts": [
                {
                    "subject": "redis",
                    "predicate": "dropping",
                    "object": "postgres advisory locks",
                    "summary": "Decision: we are dropping Redis for idempotency and using Postgres advisory locks instead. Simpler ops.",
                    "confidence": 1.0,
                    "supersedes": "Let's use Redis to store idempotency keys for the payments service."
                }
            ]
        }
    elif "store idempotency keys" in user_lower:
        return {
            "facts": [
                {
                    "subject": "redis",
                    "predicate": "store idempotency keys",
                    "object": "payments service",
                    "summary": "Let's use Redis to store idempotency keys for the payments service.",
                    "confidence": 1.0,
                    "supersedes": None
                }
            ]
        }
    elif "friday june 13" in user_lower:
        return {
            "facts": [
                {
                    "subject": "refunds",
                    "predicate": "shipping",
                    "object": "Friday June 13",
                    "summary": "Refunds feature will ship Friday June 13.",
                    "confidence": 1.0,
                    "supersedes": None
                }
            ]
        }
    elif "monday june 16" in user_lower:
        return {
            "facts": [
                {
                    "subject": "refunds",
                    "predicate": "shipping",
                    "object": "Monday June 16",
                    "summary": "Update: refunds moved to Monday June 16 after QA found a rounding bug.",
                    "confidence": 1.0,
                    "supersedes": "Refunds feature will ship Friday June 13."
                }
            ]
        }
    elif "180k base" in user_lower:
        return {
            "facts": [
                {
                    "subject": "compensation",
                    "predicate": "eng salary",
                    "object": "180k base",
                    "summary": "Comp for the new senior eng is 180k base, do not share outside leadership.",
                    "confidence": 1.0,
                    "supersedes": None
                }
            ]
        }
    elif "writing voice" in user_lower or "brand voice" in user_lower:
        return {
            "facts": [
                {
                    "subject": "brand voice",
                    "predicate": "writing voice",
                    "object": "warm, direct, and concrete",
                    "summary": "Our writing voice is warm, direct, and concrete. Avoid hype words.",
                    "confidence": 1.0,
                    "supersedes": None
                }
            ]
        }
    return {"facts": []}

def chat_json(system, user):
    has_key = os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not has_key:
        print("[Company Brain] No API key detected. Running in offline mock mode.", file=sys.stderr)
        return get_mock_response(system, user)

    try:
        resp = completion(model=LLM_MODEL,
            messages=[{"role":"system","content":system},
                      {"role":"user","content":user}],
            temperature=0.1, response_format={"type":"json_object"})
        text = resp.choices[0].message.content
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            i = min((text.find("["), text.find("{")), key=lambda x:(x<0,x))
            return json.loads(text[i:])
    except Exception as e:
        print(f"[Company Brain] LLM Call failed: {e}. Falling back to mock data.", file=sys.stderr)
        return get_mock_response(system, user)
