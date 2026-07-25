"""
MCP server for Company Brain.

Register in Claude Code (~/.claude/settings.json):

  {
    "mcpServers": {
      "company-brain": {
        "command": "python",
        "args": ["-m", "brain.wrappers.mcp_server"],
        "cwd": "/absolute/path/to/company-brain",
        "env": {
          "BRAIN_TEAM": "engineering"
        }
      }
    }
  }

For Cursor (settings.json → "mcpServers" key): same structure.

Available tools (all return JSON strings):
  search_company_facts      — semantic + FTS retrieval for your team
  get_decision_history      — ordered timeline of decisions about a subject
  record_fact               — store a trusted human-authored fact
  correct_fact              — invalidate a wrong fact and record the correction
  harvest_from_response     — extract and store facts from an AI response
  list_teams                — list all registered teams
"""
import json
from mcp.server.fastmcp import FastMCP
from brain.engine import Brain

# Single Brain instance per MCP process (connection is kept open).
# Team comes from BRAIN_TEAM env var; override per-call with team_id.
_brain = Brain()
mcp = FastMCP("company-brain")


def _b(team_id: str = None) -> Brain:
    """Return a Brain scoped to team_id if given, else the default."""
    if team_id and team_id != _brain.team_id:
        return Brain(team_id=team_id)
    return _brain


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_company_facts(
    query: str,
    team_id: str = None,
    cross_team: bool = False,
) -> str:
    """
    Search company memory for facts relevant to the query.

    Args:
        query:      Natural-language question or topic.
        team_id:    Scope to a specific team (default: caller's team from BRAIN_TEAM env).
        cross_team: If true, search across ALL teams (use for leadership / cross-cutting queries).

    Returns JSON with keys:
        answer            — short header ("From company memory:" or "I don't have...")
        synthesized_answer — full LLM-synthesized answer with cited fact IDs
        facts             — list of {summary, confidence, status, fact_id, team, source}
    """
    try:
        result = _b(team_id).search(query, surface="mcp", cross_team=cross_team)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e), "facts": []})


@mcp.tool()
def get_decision_history(
    subject: str,
    team_id: str = None,
    cross_team: bool = False,
) -> str:
    """
    Ordered timeline of every decision about a subject, including superseded ones.

    Useful for: "what changed about the launch date?", "how did we decide on the DB?"

    Args:
        subject:    The thing to trace history for (e.g. "launch date", "database choice").
        team_id:    Scope to a specific team.
        cross_team: Include superseded / invalidated facts from all teams.

    Returns JSON with keys:
        subject   — echoed back
        timeline  — list of {summary, status, confidence, fact_id, team}
                    ordered oldest-first; status="stale" means superseded
    """
    try:
        result = _b(team_id).decision_history(subject, cross_team=cross_team)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e), "timeline": []})


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@mcp.tool()
def record_fact(
    text: str,
    shared: bool = False,
    team_id: str = None,
) -> str:
    """
    Record a trusted, human-authored fact directly into company memory.

    Use this when you or the user states something that should be remembered
    (a decision, commitment, or piece of ownership) that isn't already captured.

    Args:
        text:     One clear sentence describing the fact.
                  Example: "Alice owns the billing integration."
        shared:   True = fact is company-wide (visible to all teams).
                  False (default) = scoped to the caller's team.
        team_id:  Override which team owns this fact.

    Returns JSON: {saved: fact_id, summary: text}
    """
    try:
        visibility = "shared" if shared else "team"
        result = _b(team_id).record(text, visibility=visibility)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def correct_fact(
    fact_id: str,
    correction: str,
) -> str:
    """
    Invalidate a wrong fact and replace it with the correct one.

    Use when you know a specific fact_id from search results is incorrect.
    The old fact is superseded (kept in history), not deleted.

    Args:
        fact_id:    The ID of the incorrect fact (from search results).
        correction: The correct version as a full sentence.

    Returns JSON: {corrected: old_fact_id, new_fact: new_fact_id}
    """
    try:
        result = _brain.correct(fact_id, correction)
        return json.dumps(result)
    except Exception as e:
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
    assertions (decisions, ownership, dates, technical choices).
    The harvest prompt only captures facts with confidence >= 0.7 and
    runs near-duplicate detection to avoid storing redundant facts.

    Args:
        response_text: The assistant response text to harvest from.
        session_id:    Optional — the current session/conversation ID for tracing.
        team_id:       Team to scope harvested facts to.

    Returns JSON: {harvested: N, source_kind: "ai-tool"}
    """
    try:
        result = _b(team_id).harvest(
            response_text,
            source_kind="ai-tool",
            session_id=session_id,
        )
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e), "harvested": 0})


# ---------------------------------------------------------------------------
# Meta tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_teams() -> str:
    """List all registered teams in the workspace."""
    try:
        return json.dumps(_brain.list_teams())
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
