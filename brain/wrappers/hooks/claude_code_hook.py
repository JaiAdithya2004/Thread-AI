"""
Thread lifecycle hook for Claude Code (and Cursor).

Two events handled:

  UserPromptSubmit — fires before the user's prompt reaches Claude.
    1. Searches company memory for facts relevant to the prompt.
    2. Injects them as additionalContext so Claude has them automatically.
    3. Logs every injection to the ledger (surface="hook-inject").
    4. Skips injection if fewer than _MIN_FACTS facts meet the confidence bar.

  Stop — fires after Claude finishes responding.
    1. Reads last_assistant_message.
    2. Runs the harvest extractor — extracts company-specific assertions.
    3. Stores net-new facts tagged source_kind="agent".
    4. Near-duplicate check (inside extract_from_text) prevents noise.
    5. Logs count to stderr and returns {} (never blocks the event).

Exit 0 always — we never block the user.  Errors are logged to stderr only.

═══════════════════════════════════════════════════════════════════════════════
SETUP  (~/.claude/settings.json)
═══════════════════════════════════════════════════════════════════════════════

{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python",
            "args": [
              "C:/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"
            ],
            "timeout": 15
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python",
            "args": [
              "C:/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"
            ],
            "timeout": 30
          }
        ]
      }
    ]
  }
}

On Windows use forward slashes or double-backslashes in the path.
Set BRAIN_TEAM to your team name in the environment or a .env file.
═══════════════════════════════════════════════════════════════════════════════
"""
import json
import sys

# ─── tunables ─────────────────────────────────────────────────────────────────
_MIN_FACTS = 1        # don't inject if fewer than this many facts pass the bar
_MAX_INJECT = 8       # cap facts injected per prompt (token budget)
_MIN_CONF   = 0.4     # minimum fact confidence to inject
_MAX_CONTEXT_CHARS = 3000   # Claude Code caps hook output at ~10 000 chars
# ──────────────────────────────────────────────────────────────────────────────


def _brain():
    """Lazy import so the hook process starts fast (embedding model is cached)."""
    from brain.engine import Brain
    return Brain()


def _handle_prompt_submit(payload: dict) -> dict:
    """
    Inject relevant company facts into the prompt context and log the injection.

    Returns a Claude Code UserPromptSubmit response dict with additionalContext,
    or {} if nothing is worth injecting.
    """
    prompt     = payload.get("prompt", "").strip()
    session_id = payload.get("session_id", "")

    if not prompt:
        return {}

    try:
        b = _brain()

        # ── search ──────────────────────────────────────────────────────────
        result = b.search(prompt, surface="hook-inject")
        raw_facts = result.get("facts", [])

        # filter: confidence threshold + not stale
        facts = [
            f for f in raw_facts
            if (f.get("confidence") or 0) >= _MIN_CONF
            and f.get("status") != "stale"
        ][:_MAX_INJECT]

        if len(facts) < _MIN_FACTS:
            return {}

        # ── build context block ─────────────────────────────────────────────
        lines = []
        for f in facts:
            conf  = f.get("confidence", 0)
            team  = f.get("team") or "shared"
            src   = f.get("source") or "?"
            fid   = f.get("fact_id", "")
            lines.append(
                f"• {f['summary']}"
                f"  [conf={conf:.0%}, team={team}, src={src}, id={fid}]"
            )

        context = (
            "═══ THREAD COMPANY MEMORY ═══\n"
            "Relevant facts injected automatically — cite fact IDs when used.\n"
            + "\n".join(lines)
            + "\n═══════════════════════════"
        )
        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS] + "\n…(truncated)"

        # ── log the injection to the ledger ─────────────────────────────────
        # retrieve.run() already calls db.log() on the search above (surface="hook-inject")
        # so the ledger entry is already written.  Log to stderr for local visibility.
        print(
            f"[thread-hook] injected {len(facts)} fact(s) for session {session_id!r}",
            file=sys.stderr,
        )

        return {"additionalContext": context}

    except Exception as e:
        print(f"[thread-hook] inject error: {e}", file=sys.stderr)
        return {}


def _handle_stop(payload: dict) -> dict:
    """
    Harvest company facts from the assistant's completed response.

    Facts are tagged source_kind="agent", run through the near-duplicate
    check, and stored only if they are net-new.  Never blocks the event.
    """
    response_text = payload.get("last_assistant_message", "").strip()
    session_id    = payload.get("session_id", "")

    if not response_text:
        return {}

    try:
        b      = _brain()
        result = b.harvest(
            response_text,
            source_kind="agent",
            session_id=session_id or None,
        )
        n = result.get("harvested", 0)
        if n:
            print(
                f"[thread-hook] harvested {n} fact(s) from agent response"
                f" (session={session_id!r})",
                file=sys.stderr,
            )
        else:
            print("[thread-hook] Stop: no net-new facts (low confidence or near-duplicate)", file=sys.stderr)

    except Exception as e:
        print(f"[thread-hook] harvest error: {e}", file=sys.stderr)

    return {}


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    event = payload.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        out = _handle_prompt_submit(payload)
    elif event == "Stop":
        out = _handle_stop(payload)
    else:
        out = {}

    if out:
        print(json.dumps(out))

    sys.exit(0)


if __name__ == "__main__":
    main()
