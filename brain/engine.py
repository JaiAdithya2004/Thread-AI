"""
The only public surface of the brain. CLI, MCP, hooks, and (later) the hosted
API all go through Brain. Hosted version: pass real workspace_id + caller from
auth instead of config defaults. Nothing else changes.
"""
from brain.core import db, ingest, extract, retrieve
from brain.core.connectors import REGISTRY
from brain import config


class Brain:
    def __init__(self, workspace_id=None, team_id=None, caller=None, principals=None):
        self.ws = workspace_id or config.WORKSPACE_ID
        self.team_id = team_id or config.TEAM_ID
        self.caller = caller or config.CALLER
        self.principals = principals or config.CALLER_PRINCIPALS
        self.conn = db.connect()
        db.migrate(self.conn)

    # ---- teams ----
    def create_team(self, team_id: str, name: str, description: str = "") -> dict:
        db.ensure_team(self.conn, self.ws, team_id, name, description)
        return {"team_id": team_id, "name": name}

    def list_teams(self) -> list:
        return db.list_teams(self.conn, self.ws)

    # ---- ingest (batch / event) ----
    def backfill(self, source: str, team_id: str = None) -> dict:
        """Pull full history from a connector, normalize, dedupe, classify, extract."""
        tid = team_id or self.team_id
        # Auto-register the team so the sidebar always shows it
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())
        conn = REGISTRY[source]()
        n_ep = ingest.run(self.conn, self.ws, conn.backfill(), team_id=tid)
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "episodes": n_ep, "facts": n_fa}

    def sync(self, source: str, team_id: str = None) -> dict:
        tid = team_id or self.team_id
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())
        conn = REGISTRY[source]()
        n_ep = ingest.run(self.conn, self.ws, conn.poll(), team_id=tid)
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "new_episodes": n_ep, "new_facts": n_fa}

    def ingest_event(self, source: str, raw: dict, team_id: str = None) -> dict:
        """Webhook path: one live item."""
        tid = team_id or self.team_id
        conn = REGISTRY[source]()
        ep = conn.normalize(raw)
        ep.setdefault("team_id", tid)
        n_ep = ingest.run(self.conn, self.ws, [ep], team_id=tid)
        n_fa = extract.run(self.conn, self.ws)
        return {"new_episodes": n_ep, "new_facts": n_fa}

    # ---- serve (live) ----
    def search(self, query: str, surface="mcp", cross_team: bool = False) -> dict:
        facts = retrieve.run(self.conn, self.ws, query, self.principals,
                             self.caller, surface, team_id=self.team_id,
                             cross_team=cross_team)
        return retrieve.format_answer(query, facts)

    def decision_history(self, subject: str, cross_team: bool = False) -> dict:
        facts = retrieve.run(self.conn, self.ws, f"decisions about {subject}",
                             self.principals, self.caller, "history",
                             team_id=self.team_id, include_invalid=True,
                             cross_team=cross_team)
        return retrieve.format_history(subject, facts)

    def correct(self, fact_id: str, correction: str) -> dict:
        return extract.correct_fact(self.conn, self.ws, fact_id, correction,
                                    self.principals, team_id=self.team_id)

    def record(self, text: str, acl=None, visibility: str = "team") -> dict:
        return extract.record_human_fact(self.conn, self.ws, text,
                                         acl or self.principals,
                                         team_id=self.team_id,
                                         visibility=visibility)

    def ledger(self, limit=20):
        return db.read_ledger(self.conn, self.ws, limit, team_id=self.team_id)
