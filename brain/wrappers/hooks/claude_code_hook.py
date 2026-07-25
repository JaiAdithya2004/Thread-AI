"""
Claude Code lifecycle hook for Company Brain.

Two events handled:

  UserPromptSubmit — fires before the user's prompt reaches Claude.
    Reads the prompt, searches company memory, injects relevant facts as
    additionalContext so Claude has them WITHOUT the user needing to call a tool.
    Every injection is logged to the ledger.

  Stop — fires after Claude finishes responding.
    Reads last_assistant_message, runs the harvest extractor to pick up any
    company facts the assistant asserted, stores them with source_kind="ai-tool".
    Near-duplicate check prevents noise accumulation.

Wire both in ~/.claude/settings.json (see SETUP below).
The script reads one JSON payload from stdin and writes one JSON response to stdout.
Exit 0 always — we never block the user; errors are printed to stderr and ignored.

──────────────────────────────────────────────────────────────
SETUP  (~/.claude/settings.json)
──────────────────────────────────────────────────────────────
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
              "C:/absolute/path/to/company-brain/brain/wrappers/hooks/claude_code_hook.py"
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
              "C:/absolute/path/to/company-brain/brain/wrappers/hooks/claude_code_hook.py"
            ],
            "timeout": 30
          }
        ]
      }
    ]
  }
}
──────────────────────────────────────────────────────────────

On Windows replace forward slashes with double backslashes OR use forward slashes
(Python accepts both). Set BRAIN_TEAM env var to your team name if needed.
"""
import json
import sys

# Minimum facts to bother injecting (avoids a noisy "1 weak fact" injection)
_MIN_FACTS = 1
# Max facts to inject as context (keep tokens lean)
_MAX_INJECT = 8
# Min confidence for a fact to be injected as context
_MIN_CONF = 0.4
# Max chars of injected context block (Claude Code caps hook output at 10 000)
_MAX_CONTEXT_CHARS = 3000


def _brain():
    """Lazy import so the hook process starts fast even if embedding model is cached."""
    from brain.engine import Brain
    return Brain()


def _handle_prompt_submit(payload: dict) -> dict:
    """
    Inject relevant company facts into the prompt context.
    Returns a Claude Code UserPromptSubmit response dict.
    """
    prompt = payload.get("prompt", "").strip()
    session_id = payload.get("session_id", "")

    if not prompt:
        return {}

    try:
        b = _brain()
        result = b.search(prompt, surface="hook")
        facts = [
            f for f in result.get("facts", [])
            if (f.get("confidence") or 0) >= _MIN_CONF
               and f.get("status") != "stale"
        ][:_MAX_INJECT]

        if len(facts) < _MIN_FACTS:
            return {}

        lines = []
        for f in facts:
            conf = f.get("confidence", 0)
            team = f.get("team") or "shared"
            lines.append(f"• {f['summary']}  [conf={conf:.0%}, team={team}]")

        context = (
            "COMPANY MEMORY — relevant facts for this session:\n"
            + "\n".join(lines)
        )
        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS] + "\n…(truncated)"

        return {"additionalContext": context}

    except Exception as e:
        print(f"[brain-hook] prompt inject error: {e}", file=sys.stderr)
        return {}


def _handle_stop(payload: dict) -> dict:
    """
    Harvest company facts from the assistant's last response.
    Returns an empty dict (never block the Stop event).
    """
    response_text = payload.get("last_assistant_message", "").strip()

    if not response_text:
        return {}

    try:
        b = _brain()
        result = b.harvest(
            response_text,
            source_kind="ai-tool",
            session_id=payload.get("session_id"),
        )
        n = result.get("harvested", 0)
        if n:
            print(f"[brain-hook] harvested {n} fact(s) from agent response", file=sys.stderr)
    except Exception as e:
        print(f"[brain-hook] harvest error: {e}", file=sys.stderr)

    return {}


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Unexpected format — pass through silently
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
