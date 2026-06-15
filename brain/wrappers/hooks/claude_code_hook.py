"""
Lifecycle hook for Claude Code / Cursor / Codex.

Two jobs (Hyper-style):
  1) on prompt submit: inject the 5-15 most relevant company facts automatically,
     so the agent has context WITHOUT calling a tool.
  2) on agent response: harvest interesting new facts back into the brain.

Every injection is logged to the ledger (glass-box). Wire this as a
UserPromptSubmit / Stop hook in your tool's hook config. It reads the prompt on
stdin and prints context to stdout.
"""
import sys, json
from brain.engine import Brain

def on_prompt_submit(prompt: str) -> str:
    res = Brain().search(prompt, surface="hook")
    lines = [f"- {f['summary']} [{f['status']}, conf {f['confidence']}]"
             for f in res.get("facts", [])]
    if not lines:
        return ""
    return "Relevant company context:\n" + "\n".join(lines)

def on_agent_response(text: str):
    # Optional: extract new facts from the agent's output and store them.
    # Keep conservative to avoid noise. Stub for now.
    pass

if __name__ == "__main__":
    payload = sys.stdin.read()
    try:
        prompt = json.loads(payload).get("prompt", payload)
    except json.JSONDecodeError:
        prompt = payload
    out = on_prompt_submit(prompt)
    if out:
        print(out)
