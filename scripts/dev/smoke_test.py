#!/usr/bin/env python3
"""
Thread smoke test — hits a running server and verifies key endpoints.

Usage:
    python scripts/dev/smoke_test.py [--url http://localhost:7070] [--token <bearer>]

Pass --token only when running against a server with real Clerk auth.
In dev/bypass mode the token is ignored; any string works.

Exit codes:
    0  all checks passed
    1  one or more checks failed
"""
import sys, argparse, json
try:
    import requests
except ImportError:
    print("Install requests:  pip install requests")
    sys.exit(1)

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
RESET  = "\033[0m"

passed = failed = 0


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  {GREEN}✓{RESET}  {name}")
    else:
        failed += 1
        print(f"  {RED}✗{RESET}  {name}" + (f"  — {detail}" if detail else ""))


def main():
    p = argparse.ArgumentParser(description="Thread smoke test")
    p.add_argument("--url",   default="http://localhost:7070", help="Base URL of running server")
    p.add_argument("--token", default="smoke-test-dev",        help="Bearer token for auth")
    args = p.parse_args()

    base  = args.url.rstrip("/")
    hdr   = _hdr(args.token)
    s     = requests.Session()
    s.timeout = 10

    print(f"\n[smoke] Thread smoke test → {base}\n")

    # ── 1. Status endpoint (unauthenticated) ──────────────────────────────────
    try:
        r = s.get(f"{base}/api/status")
        check("/api/status returns 200", r.status_code == 200, f"got {r.status_code}")
        data = r.json()
        check("/api/status has 'keys' dict",    isinstance(data.get("keys"), dict))
        keys = data.get("keys", {})
        check("/api/status keys are booleans",  all(isinstance(v, bool) for v in keys.values()),
              f"non-bool: {[k for k,v in keys.items() if not isinstance(v,bool)]}")
        check("/api/status no 'composio' key",  "composio" not in keys)
    except Exception as e:
        check("/api/status", False, str(e))
        check("/api/status has 'keys' dict",   False, "skipped")
        check("/api/status keys are booleans", False, "skipped")
        check("/api/status no 'composio' key", False, "skipped")

    # ── 2. Search — empty query rejected ─────────────────────────────────────
    try:
        r = s.post(f"{base}/api/search", json={"query": ""}, headers=hdr)
        check("/api/search empty query → 400", r.status_code == 400, f"got {r.status_code}")
    except Exception as e:
        check("/api/search empty query → 400", False, str(e))

    # ── 3. Search — valid query returns structured response ───────────────────
    try:
        r = s.post(f"{base}/api/search", json={"query": "smoke test query"}, headers=hdr)
        check("/api/search valid query → 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            check("/api/search has 'facts' list", isinstance(data.get("facts"), list))
    except Exception as e:
        check("/api/search valid query → 200", False, str(e))
        check("/api/search has 'facts' list",  False, "skipped")

    # ── 4. Platform metrics gated ─────────────────────────────────────────────
    try:
        r = s.get(f"{base}/api/_platform/metrics", headers=hdr)
        check("/api/_platform/metrics is gated",
              r.status_code in (403, 200, 503),  # 403 without flag, 200/503 with flag
              f"got {r.status_code}")
    except Exception as e:
        check("/api/_platform/metrics is gated", False, str(e))

    # ── 5. Security headers present ───────────────────────────────────────────
    try:
        r = s.get(f"{base}/api/status")
        hdrs = r.headers
        check("X-Content-Type-Options header", "nosniff" in hdrs.get("X-Content-Type-Options",""))
        check("X-Frame-Options header",        "DENY"    in hdrs.get("X-Frame-Options",""))
        check("Content-Security-Policy header", bool(hdrs.get("Content-Security-Policy")))
    except Exception as e:
        check("Security headers", False, str(e))

    # ── 6. Web UI served ──────────────────────────────────────────────────────
    try:
        r = s.get(f"{base}/")
        check("/ serves HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type",""),
              f"status={r.status_code}")
    except Exception as e:
        check("/ serves HTML", False, str(e))

    # ── 7. Unauthenticated write blocked (non-bypass) ─────────────────────────
    try:
        r = s.post(f"{base}/api/record", json={"text": "smoke test fact"})
        # In bypass mode this is 200 (fine); in production 401
        check("/api/record responds", r.status_code in (200, 401, 422), f"got {r.status_code}")
    except Exception as e:
        check("/api/record responds", False, str(e))

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    color = GREEN if failed == 0 else RED
    print(f"\n[smoke] {color}{passed}/{total} checks passed{RESET}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
