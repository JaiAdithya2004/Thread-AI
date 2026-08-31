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

    def close(self):
        """Return the DB connection to the pool."""
        if self.conn and not self.conn.closed:
            self.conn.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- plan limit helpers ----
    def _plan(self) -> dict:
        """Return plan dict for this account; free defaults if no postgres / no account."""
        from brain.core.db import BACKEND
        if BACKEND != "postgres" or not self.account_id:
            return {"name": "free", "monthly_credits": 500, "max_seats": 1,
                    "max_connectors": 0, "max_backfill": 500}
        from brain.core.db_pg import get_plan_for_account
        return get_plan_for_account(self.account_id)

    def _check_seat_limit(self):
        from brain.core.db import BACKEND
        if BACKEND != "postgres" or not self.account_id:
            return
        plan = self._plan()
        max_seats = plan.get("max_seats", 1)
        from brain.core.db_pg import get_seat_count
        seats = get_seat_count(self.account_id)
        if seats >= max_seats:
            raise ValueError(
                f"Seat limit reached ({seats}/{max_seats}). Upgrade your plan to add more members."
            )

    def _check_connector_limit(self, existing_count: int):
        plan = self._plan()
        max_conn = plan.get("max_connectors", 0)
        if existing_count >= max_conn:
            raise ValueError(
                f"Integration limit reached ({existing_count}/{max_conn}). "
                "Upgrade your plan to connect more integrations."
            )

    def _check_backfill_limit(self, items_ingested: int):
        plan = self._plan()
        max_bf = plan.get("max_backfill", 500)
        if items_ingested > max_bf:
            raise ValueError(
                f"Backfill item limit exceeded ({items_ingested}/{max_bf}). "
                "Upgrade your plan for larger backfills."
            )

    # ---- teams ----
    def create_team(self, team_id: str, name: str, description: str = "") -> dict:
        db.ensure_team(self.conn, self.ws, team_id, name, description)
        return {"team_id": team_id, "name": name}

    def list_teams(self) -> list:
        return db.list_teams(self.conn, self.ws)

    # ---- async worker helpers ----
    def _async_enqueue(self, job_name: str, **kwargs) -> bool:
        """
        Try to enqueue via arq. Falls back to sync extraction if Redis is unavailable.
        Never raises — caller always gets a bool indicating whether work was queued.
        """
        try:
            import asyncio
            import arq
            from arq.connections import RedisSettings

            async def _enqueue():
                pool = await arq.create_pool(RedisSettings.from_dsn(config.REDIS_URL))
                await pool.enqueue_job(job_name, **kwargs)
                await pool.aclose()

            loop = asyncio.new_event_loop()
            loop.run_until_complete(_enqueue())
            loop.close()
            return True
        except Exception as _e:
            import sys
            print(f"[engine] arq unavailable ({_e}), falling back to sync", file=sys.stderr)
            if job_name == "extract_facts":
                extract.run(self.conn, kwargs.get("ws", self.ws))
            return False

    # ---- ingest (batch / event) ----
    def backfill(self, source: str, team_id: str = None) -> dict:
        """
        Pull full history from a connector, normalize, dedupe, ingest, extract.
        Connector failures are isolated — a broken connector returns 0 episodes
        and logs the error rather than raising and killing the request.
        Ingestion and extraction are fully idempotent (content-hash dedup +
        upsert semantics on facts).
        """
        import sys
        tid = team_id or self.team_id
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())

        from brain.core.connectors.composio import set_billing_context, clear_billing_context
        set_billing_context(self.account_id or "")

        try:
            # Pass caller identity to connectors that support per-user isolation
            connector = REGISTRY[source](user_id=self.caller) if source == "composio" else REGISTRY[source]()
            episodes = connector.backfill()
        except Exception as e:
            print(f"[engine] connector '{source}' backfill failed: {e}", file=sys.stderr)
            clear_billing_context()
            return {"source": source, "team": tid, "episodes": 0, "facts": 0,
                    "error": str(e)}

        # ingest is idempotent — content-hash dedup prevents duplicates on re-run
        n_ep = ingest.run(self.conn, self.ws, episodes, team_id=tid, trigger="backfill")
        clear_billing_context()

        try:
            self._check_backfill_limit(n_ep)
        except ValueError as e:
            print(f"[engine] backfill limit warning: {e}", file=sys.stderr)

        if USE_ASYNC_WORKER:
            enqueued = self._async_enqueue("extract_facts", ws=self.ws, team_id=tid)
            return {"source": source, "team": tid, "episodes": n_ep, "queued": enqueued}
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "episodes": n_ep, "facts": n_fa}

    def sync(self, source: str, team_id: str = None, since=None) -> dict:
        """Incremental poll. `since` is a datetime watermark from connector_sync_state.
        When None the connector defaults to the last 24h."""
        import sys
        tid = team_id or self.team_id
        db.ensure_team(self.conn, self.ws, tid, tid.replace("-", " ").title())

        from brain.core.connectors.composio import set_billing_context, clear_billing_context
        set_billing_context(self.account_id or "")

        try:
            if source == "composio":
                connector = REGISTRY[source](user_id=self.caller)
                episodes = connector.poll(since=since)
            else:
                connector = REGISTRY[source]()
                episodes = connector.poll(since=since) if since is not None else connector.poll()
        except Exception as e:
            print(f"[engine] connector '{source}' sync failed: {e}", file=sys.stderr)
            clear_billing_context()
            return {"source": source, "team": tid, "new_episodes": 0, "new_facts": 0,
                    "error": str(e)}

        n_ep = ingest.run(self.conn, self.ws, episodes, team_id=tid, trigger="poll")
        clear_billing_context()

        if USE_ASYNC_WORKER:
            enqueued = self._async_enqueue("extract_facts", ws=self.ws, team_id=tid)
            return {"source": source, "team": tid, "new_episodes": n_ep, "queued": enqueued}
        n_fa = extract.run(self.conn, self.ws)
        return {"source": source, "team": tid, "new_episodes": n_ep, "new_facts": n_fa}

    def ingest_event(self, source: str, raw: dict, team_id: str = None) -> dict:
        """Webhook path: one live item. Always async-extracts if worker is enabled."""
        tid = team_id or self.team_id
        connector = REGISTRY[source](user_id=self.caller) if source == "composio" else REGISTRY[source]()
        ep = connector.normalize(raw)
        ep.setdefault("team_id", tid)
        n_ep = ingest.run(self.conn, self.ws, [ep], team_id=tid, trigger="webhook")
        if n_ep > 0:
            if USE_ASYNC_WORKER:
                self._async_enqueue("extract_facts", ws=self.ws, team_id=tid)
                return {"new_episodes": n_ep, "queued": True}
            n_fa = extract.run(self.conn, self.ws)
            return {"new_episodes": n_ep, "new_facts": n_fa}
        return {"new_episodes": 0, "new_facts": 0}

    # ---- serve (live) ----
    def search(self, query: str, surface="mcp", cross_team: bool = False) -> dict:
        facts = retrieve.run(self.conn, self.ws, query, self.principals,
                             self.caller, surface, team_id=self.team_id,
                             cross_team=cross_team)
        return retrieve.format_answer(query, facts, conn=self.conn, ws=self.ws)

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
