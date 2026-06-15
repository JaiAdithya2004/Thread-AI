# Company Brain (Hyper-style, horizontal)

Connect Slack, Gmail, Google Docs, Calendar, Notion. Ingest everything, build a
self-maintaining knowledge graph of facts (with supersession + ACL + provenance),
and serve that context to AI tools over MCP and lifecycle hooks. Mirrors Hyper's
architecture. See hyper-clone-architecture-spec.md for the full design.

## Run the offline demo (no credentials needed)
    python -m venv .venv && source .venv/bin/activate
    pip install -e .
    export OPENAI_API_KEY=...          # or BRAIN_LLM_MODEL=ollama/llama3.1
    brain backfill sample              # loads sample_data/, extracts facts
    brain search "why did we drop redis?"
    brain search "when do refunds ship?"
    brain history "refunds"            # timeline incl. superseded "Friday" fact
    brain log

## Permissions demo
The sample comp fact (id 1005) is acl=["leadership"]. With default principals you
won't see it; set BRAIN_PRINCIPALS=leadership and you will. ACL is filtered
BEFORE results are returned, so two users get different answers.

## Real connectors
Each file in brain/core/connectors/ implements the same interface
(backfill/poll/normalize). Fill the TODOs with the official SDK calls
(slack_sdk, google-api-python-client, notion-client) and set tokens in .env.

## Serving
- MCP: python -m brain.wrappers.mcp_server  (tools: search_company_facts,
  get_decision_history, correct_fact, record_fact)
- Hooks: brain/wrappers/hooks/claude_code_hook.py injects context on every prompt
  and can harvest facts from responses.

## The boundary
Everything funnels through brain/engine.py :: Brain(workspace_id, caller,
principals). Hosted multi-tenant version: pass those from auth, swap
core/db.py from SQLite to Postgres+pgvector. Wrappers/connectors unchanged.

## Status
Runnable offline via the sample connector. Real connectors are stubbed with clear
interfaces. LLM prompts need tuning on real data.
