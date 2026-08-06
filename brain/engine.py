"""
The only public surface of the brain. CLI, MCP, hooks, and the hosted
API all go through Brain. Hosted version: pass real workspace_id + caller from
auth instead of config defaults. Nothing else changes.
"""
import os
from brain.core import db, ingest, extract, retrieve
from brain.core.connectors import REGISTRY
from brain import config

USE_ASYNC_WORKER = bool(os.getenv("BRAIN_ASYNC_WORKER", ""))


class Brain:
    def __init__(self, workspace_id=None, team_id=None, caller=None, principals=None,
                 account_id=None):
        self.ws         = workspace_id or config.WORKSPACE_ID
        self.team_id    = team_id or config.TEAM_ID
        self.caller     = caller or config.CALLER
        self.principals = principals or config.CALLER_PRINCIPALS
        self.account_id = account_id  # set by API layer for billing; None in dev/bypass
        self.conn = db.connect()
        db.migrate(self.conn)

    # ---- teams ----
    def create_team(self, team_id: str, name: str, description: str = "") -> dict:
        db.ensure_team(self.conn, self.ws, team_id, name, description)
        return {"team_id": team_id, "name": name}

    def list_teams(self) -> list:
        return db.list_teams(self.conn, self.ws)

    # ---- async worker helpers ----
    def _async_enqueue(self, job_name: str, **kwargs) -> bool:
        """Try to enqueue via arq. Falls back to sync if Redis unavailable. Never raises."""
        try:
            import asyncio, arq
            from arq.connections import RedisSettings

            async def _enqueue():
                pool = await arq.create_pool(RedisSettings.from_dsn(config.REDIS_URL))
                await pool.enqueue_job(job_name, **kwargs)
                await pool.aclose()

            loop = asyncio.new_event_loop()
            loop.run_until_complete(_enqueue())
            loop.close()
            return True
        except Exception:
            if job_name == "extract_facts":
                extract.run(self.conn, kwargs.get("ws", self.ws))
            return False

    # ---- ingest (batch / event) ----
    def backfill(self, source: str, team_id: str = None) -> dict:
        """Pull full history from a connector, normalize, dedupe, classify, extract."""
        tid = team_id or self.team_id
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())

        # Set billing context for connector metering
        from brain.core.connectors.composio import set_billing_context, clear_billing_context
        set_billing_context(self.account_id or "")

        connector = REGISTRY[source]()
        n_ep = ingest.run(self.conn, self.ws, connector.backfill(), team_id=tid)
        clear_billing_context()

        if USE_ASYNC_WORKER:
            enqueued = self._async_enqueue("extract_facts", ws=self.ws, team_id=tid)
            return {"source": source, "team": tid, "episodes": n_ep, "queued": enqueued}
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "episodes": n_ep, "facts": n_fa}

    def sync(self, source: str, team_id: str = None) -> dict:
        tid = team_id or self.team_id
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())

        from brain.core.connectors.composio import set_billing_context, clear_billing_context
        set_billing_context(self.account_id or "")

        connector = REGISTRY[source]()
        n_ep = ingest.run(self.conn, self.ws, connector.poll(), team_id=tid)
        clear_billing_context()

        if USE_ASYNC_WORKER:
            enqueued = self._async_enqueue("extract_facts", ws=self.ws, team_id=tid)
            return {"source": source, "team": tid, "new_episodes": n_ep, "queued": enqueued}
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "new_episodes": n_ep, "new_facts": n_fa}

    def ingest_event(self, source: str, raw: dict, team_id: str = None) -> dict:
        """Webhook path: one live item."""
        tid = team_id or self.team_id
        connector = REGISTRY[source]()
        ep = connector.normalize(raw)
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

    def harvest(self, text: str, source_kind: str = "ai-tool",
                session_id: str = None) -> dict:
        n = extract.extract_from_text(
            self.conn, self.ws, text,
            source_kind=source_kind,
            team_id=self.team_id,
            session_id=session_id,
        )
        return {"harvested": n, "source_kind": source_kind}

    def ledger(self, limit=20):
        return db.read_ledger(self.conn, self.ws, limit, team_id=self.team_id)

    # ---- cost & usage reporting ----
    def usage_summary(self, period: str = None) -> dict:
        """
        Return cost and credit usage for this account, broken down by task and team.
        period: 'YYYY-MM' string; defaults to current month.
        Returns: {period, tokens, cost_usd, credits_used, credits_allowance,
                  by_task: [{task, prompt_tokens, completion_tokens, cost_usd}],
                  by_team: [{team_id, cost_usd}],
                  integrations: {api_calls, backfill_items, connected_accounts}}
        """
        from brain.core.db import BACKEND
        if BACKEND != "postgres" or not self.account_id:
            return {"period": period, "tokens": 0, "cost_usd": 0.0,
                    "credits_used": 0, "credits_allowance": None,
                    "by_task": [], "by_team": [], "integrations": {}}

        import datetime as _dt
        from brain.core import db_pg as _pg
        from brain.config import CREDIT_USD_COST, PLAN_CREDITS

        now = _dt.date.today()
        if period:
            try:
                y, m = map(int, period.split("-"))
                period_start = _dt.date(y, m, 1)
            except Exception:
                period_start = now.replace(day=1)
        else:
            period_start = now.replace(day=1)
            period = period_start.strftime("%Y-%m")

        # End of month
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1, day=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1, day=1)

        conn = _pg.connect()
        try:
            cur = conn.cursor()

            # Aggregate LLM usage
            cur.execute("""
                SELECT
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens,
                    COALESCE(SUM(cost_usd), 0.0)                        AS total_cost,
                    json_agg(json_build_object(
                        'task', task,
                        'prompt_tokens', pt, 'completion_tokens', ct, 'cost_usd', cu
                    )) AS by_task
                FROM (
                    SELECT task,
                           SUM(prompt_tokens)     AS pt,
                           SUM(completion_tokens) AS ct,
                           SUM(cost_usd)          AS cu
                    FROM usage_events
                    WHERE account_id = %s
                      AND ts >= %s AND ts < %s
                    GROUP BY task
                ) t
            """, (self.account_id, period_start.isoformat(), period_end.isoformat()))
            row = cur.fetchone() or {}
            total_tokens = int(row.get("total_tokens") or 0)
            total_cost   = float(row.get("total_cost") or 0.0)
            by_task      = row.get("by_task") or []

            # By team
            cur.execute("""
                SELECT team_id, SUM(cost_usd) AS cu
                FROM usage_events
                WHERE account_id = %s AND ts >= %s AND ts < %s
                GROUP BY team_id
            """, (self.account_id, period_start.isoformat(), period_end.isoformat()))
            by_team = [{"team_id": r["team_id"], "cost_usd": float(r["cu"] or 0)}
                       for r in cur.fetchall()]

            # Integration usage
            cur.execute("""
                SELECT
                    COALESCE(SUM(api_calls), 0)         AS api_calls,
                    COALESCE(SUM(backfill_items), 0)    AS backfill_items,
                    COALESCE(MAX(connected_accounts), 0) AS connected_accounts
                FROM integration_usage
                WHERE account_id = %s AND period >= %s AND period < %s
            """, (self.account_id, period_start.isoformat(), period_end.isoformat()))
            int_row = cur.fetchone() or {}

            # Credits: look up plan allowance
            cur.execute("""
                SELECT p.name FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.account_id = %s AND s.status = 'active'
                LIMIT 1
            """, (self.account_id,))
            plan_row = cur.fetchone()
            plan_name = (plan_row or {}).get("name", "free")
            allowance = PLAN_CREDITS.get(plan_name, PLAN_CREDITS["free"])
            credits_used = int(total_cost / CREDIT_USD_COST) if CREDIT_USD_COST else 0

            cur.close()
        finally:
            conn.close()

        return {
            "period":             period,
            "tokens":             total_tokens,
            "cost_usd":           round(total_cost, 6),
            "credits_used":       credits_used,
            "credits_allowance":  allowance,
            "plan":               plan_name,
            "by_task":            by_task or [],
            "by_team":            by_team,
            "integrations": {
                "api_calls":          int(int_row.get("api_calls") or 0),
                "backfill_items":     int(int_row.get("backfill_items") or 0),
                "connected_accounts": int(int_row.get("connected_accounts") or 0),
            },
        }
