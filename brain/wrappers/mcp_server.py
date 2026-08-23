"""
Thread MCP server — agent-native company memory.

═══════════════════════════════════════════════════════════════════════════════
QUICK SETUP  (~/.claude/settings.json  OR  Cursor settings.json → mcpServers)
═══════════════════════════════════════════════════════════════════════════════

{
  "mcpServers": {
    "thread": {
      "command": "python",
      "args": ["-m", "brain.wrappers.mcp_server"],
      "cwd": "/absolute/path/to/thread",
      "env": {
        "DATABASE_URL": "postgresql://...",
        "BRAIN_TEAM": "engineering",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}

On Windows use forward slashes or escaped backslashes in "cwd".
Cursor: paste the same "mcpServers" block into your .cursor/mcp.json.

═══════════════════════════════════════════════════════════════════════════════
TOOLS
═══════════════════════════════════════════════════════════════════════════════

  search_company_facts    — semantic + FTS retrieval (primary read tool)
  get_decision_history    — ordered timeline of decisions about a subject
  record_fact             — store a trusted human-authored fact
  correct_fact            — supersede a wrong fact with the correct one
  harvest_from_response   — extract facts from an agent response (write)
  list_teams              — list all registered teams in the workspace

═══════════════════════════════════════════════════════════════════════════════
RECOMMENDED WORKFLOW (in a Claude Code / Cursor session)
═══════════════════════════════════════════════════════════════════════════════

1. Before answering a company-specific question:
     search_company_facts(query="<topic>")

2. After stating a decision or fact in your response:
     harvest_from_response(response_text="<your message>", session_id="<id>")

3. To explicitly save a decision:
     record_fact(text="We chose Postgres for the billing DB.", shared=True)

4. To fix wrong information:
     correct_fact(fact_id="<id from search>", correction="<correct version>")
"""
import json
import sys
from mcp.server.fastmcp import FastMCP
from brain.engine import Brain

# One Brain instance per MCP process (long-lived DB connection).
# Override per-call with team_id to scope to a different team.
_brain = Brain()
mcp = FastMCP("thread")


def _b(team_id: str = None) -> Brain:
    """Return a Brain scoped to team_id if given, else the default."""
    if team_id and team_id != _brain.team_id:
        return Brain(team_id=team_id)
    return _brain


# ─────────────────────────────────────────────────────────────────────────────
# Read tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_company_facts(
    query: str,
    team_id: str = None,
    cross_team: bool = False,
) -> str:
    """
    Search Thread's company memory for facts relevant to the query.

    Use this before answering any question that could touch company-specific
    knowledge: architecture decisions, ownership, deadlines, past discussions,
    or institutional context.  Results are ranked by semantic + keyword
    relevance and annotated with confidence and source.

    Args:
        query:      Natural-language question or topic, e.g.
                    "Who owns the billing integration?"
                    "What database are we using for analytics?"
        team_id:    Scope to one team (default: BRAIN_TEAM env var).
                    Pass "shared" to see company-wide facts only.
        cross_team: True = search all teams (good for leadership or
                    cross-cutting architecture queries).

    Returns JSON:
        {
          "answer": "From company memory:" | "I don't have ...",
          "synthesized_answer": "<LLM-written paragraph citing facts>",
          "facts": [
            {
              "summary":    "<one-sentence fact>",
              "confidence": 0.0-1.0,
              "status":     "valid" | "stale",
              "fact_id":    "<uuid>",
              "team":       "<team name>",
              "source":     "slack" | "notion" | "human" | "agent" | ...
            }
          ]
        }

    IMPORTANT: always cite fact_ids in your response so the user can verify.
    If synthesized_answer is empty the brain lacks enough confidence — say so.
    """
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "query must not be empty", "facts": []})
    try:
        result = _b(team_id).search(query, surface="mcp", cross_team=cross_team)
        return json.dumps(result)
    except Exception as e:
        print(f"[thread-mcp] search error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e), "facts": []})


@mcp.tool()
def get_decision_history(
    subject: str,
    team_id: str = None,
    cross_team: bool = False,
) -> str:
    """
    Return a chronological timeline of every decision about a subject,
    including superseded (stale) entries so you can see what changed and when.

    Use for questions like:
      "What changed about the launch date?"
      "How did we end up choosing the current auth provider?"
      "Has the API rate limit ever been revised?"

    Args:
        subject:    The topic to trace, e.g. "launch date", "database choice",
                    "rate limit policy".  Keep it short — this is used as the
                    search seed, not a full sentence.
        team_id:    Scope to one team.
        cross_team: Include entries from all teams.

    Returns JSON:
        {
          "subject":  "<echoed>",
          "timeline": [
            {
              "summary":    "<fact text>",
              "status":     "valid" | "stale",
              "confidence": 0.0-1.0,
              "fact_id":    "<uuid>",
              "team":       "<team>"
            }
          ]
        }

    Timeline is ordered oldest-first.  status="stale" means the fact was
    superseded by a newer one — read it to understand context, but don't act on it.
    """
    subject = (subject or "").strip()
    if not subject:
        return json.dumps({"error": "subject must not be empty", "timeline": []})
    try:
        result = _b(team_id).decision_history(subject, cross_team=cross_team)
        return json.dumps(result)
    except Exception as e:
        print(f"[thread-mcp] history error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e), "timeline": []})


# ─────────────────────────────────────────────────────────────────────────────
# Write tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def record_fact(
    text: str,
    shared: bool = False,
    team_id: str = None,
) -> str:
    """
    Record a trusted, human-authored fact directly into company memory.

    Call this when the user explicitly states something that should be
    remembered — a decision, a commitment, a change of ownership, or a
    constraint — that is not already in memory.

    Prefer one clear declarative sentence per call.
    Examples of good facts:
      "Alice owns the billing integration as of 2026-Q2."
      "We decided to use Postgres for the analytics warehouse."
      "The public API v2 launches 2026-09-01."

    Args:
        text:     The fact as one complete sentence.
        shared:   True  = visible to all teams (company-wide).
                  False = scoped to the caller's team (default).
        team_id:  Override team ownership of this fact.

    Returns JSON: {"saved": "<fact_id>", "summary": "<text>"}
    """
    text = (text or "").strip()
    if not text:
        return json.dumps({"error": "text must not be empty"})
    if len(text) > 2000:
        return json.dumps({"error": "text too long (max 2000 chars)"})
    try:
        visibility = "shared" if shared else "team"
        result = _b(team_id).record(text, visibility=visibility)
        return json.dumps(result)
    except Exception as e:
        print(f"[thread-mcp] record error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e)})


@mcp.tool()
def correct_fact(
    fact_id: str,
    correction: str,
) -> str:
    """
    Supersede an incorrect fact with the correct version.

    The old fact is invalidated (kept in history with status="stale") and
    a new fact is created in its place.  Use get_decision_history to see
    the full change trail afterward.

    When to use:
      - A search result contains information the user confirms is wrong.
      - A previously recorded fact is outdated (e.g. ownership changed).

    Args:
        fact_id:    The UUID of the wrong fact (from search "fact_id" field).
        correction: The correct version as a complete sentence.

    Returns JSON: {"corrected": "<old_fact_id>", "new_fact": "<new_fact_id>"}
    """
    fact_id = (fact_id or "").strip()
    correction = (correction or "").strip()
    if not fact_id:
        return json.dumps({"error": "fact_id must not be empty"})
    if not correction:
        return json.dumps({"error": "correction must not be empty"})
    try:
        result = _brain.correct(fact_id, correction)
        return json.dumps(result)
    except Exception as e:
        print(f"[thread-mcp] correct error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e)})


@mcp.tool()
def harvest_from_response(
    response_text: str,
    session_id: str = None,
    team_id: str = None,
) -> str:
    """
    Extract and store company facts from an AI assistant's response.

    Call this AFTER you generate a response that contains company-specific
    assertions — decisions, ownership, dates, architecture choices, or any
    other institutional knowledge.  The extractor:

      • Runs an LLM pass to identify fact-worthy sentences (confidence >= 0.7).
      • Runs near-duplicate detection — does NOT store if an identical fact
        already exists in memory.
      • Tags stored facts with source_kind="agent" for traceability.
      • Returns the count of net-new facts added.

    Args:
        response_text: The assistant's message to extract facts from.
                       Should be the complete message, not a summary.
        session_id:    Current session/conversation ID for ledger traceability.
                       Pass Claude Code's session_id if available.
        team_id:       Team to scope harvested facts to (default: BRAIN_TEAM).

    Returns JSON: {"harvested": N, "source_kind": "agent"}

    Note: harvested=0 is normal — it means no net-new high-confidence facts
    were found (either low confidence or near-duplicate of existing facts).
    """
    response_text = (response_text or "").strip()
    if not response_text:
        return json.dumps({"harvested": 0, "source_kind": "agent"})
    try:
        result = _b(team_id).harvest(
            response_text,
            source_kind="agent",
            session_id=session_id,
        )
        return json.dumps(result)
    except Exception as e:
        print(f"[thread-mcp] harvest error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e), "harvested": 0})


# ─────────────────────────────────────────────────────────────────────────────
# Meta tools
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_teams() -> str:
    """
    List all registered teams in the workspace.

    Returns JSON: [{"team_id": "...", "name": "...", "created_at": "..."}, ...]

    Use to discover valid team_id values for scoping other tool calls.
    """
    try:
        return json.dumps(_brain.list_teams())
    except Exception as e:
        print(f"[thread-mcp] list_teams error: {e}", file=sys.stderr)
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
