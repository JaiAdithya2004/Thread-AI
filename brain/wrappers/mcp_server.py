"""
MCP server. Register in Claude Code / Cursor:
  {"mcpServers": {"company-brain": {
     "command": "python", "args": ["-m","brain.wrappers.mcp_server"]}}}
Team context comes from BRAIN_TEAM env var; override per-call with cross_team flag.
"""
import json
from mcp.server.fastmcp import FastMCP
from brain.engine import Brain

brain = Brain()
mcp = FastMCP("company-brain")

@mcp.tool()
def search_company_facts(query: str, cross_team: bool = False) -> str:
    """Search company memory for the current team.
    Set cross_team=true to search across all teams (privileged)."""
    return json.dumps(brain.search(query, surface="mcp", cross_team=cross_team))

@mcp.tool()
def get_decision_history(subject: str, cross_team: bool = False) -> str:
    """Timeline of decisions about a subject, including superseded ones."""
    return json.dumps(brain.decision_history(subject, cross_team=cross_team))

@mcp.tool()
def correct_fact(fact_id: str, correction: str) -> str:
    """Hard human correction: invalidate a wrong fact and record the right one."""
    return json.dumps(brain.correct(fact_id, correction))

@mcp.tool()
def record_fact(text: str, shared: bool = False) -> str:
    """Record a trusted, human-authored fact.
    Set shared=true for company-wide facts visible to all teams."""
    visibility = "shared" if shared else "team"
    return json.dumps(brain.record(text, visibility=visibility))

@mcp.tool()
def list_teams() -> str:
    """List all teams in the workspace."""
    return json.dumps(brain.list_teams())

if __name__ == "__main__":
    mcp.run()
