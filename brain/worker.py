"""
arq async worker - runs background jobs enqueued by Brain.

Start with:
    arq brain.worker.WorkerSettings

Jobs:
    extract_facts(ws, team_id)             - pull facts from unextracted episodes
    sync_source(ws, source, team_id)       - incremental connector poll
    ingest_event_job(ws, source, raw)      - webhook fast path (single event)
    poll_all_workspaces()                  - cron: discover workspaces and enqueue syncs
    reconcile_watermarks()                 - cron: gap-healing re-poll with overlap window

Dead-letter:
    Any job that exceeds max_retries lands in the `dead_letter_jobs`
    Postgres table. The API exposes it at GET /api/admin/dead-letter.
"""
import json
import os
import sys
import datetime
import traceback as _tb
import uuid as _uuid

from brain import config
from brain.core.log import get_logger as _get_logger, set_trace_id as _set_trace_id

_wlog = _get_logger("brain.worker")

# ---------------------------------------------------------------------------
# Dead-letter helpers
# ---------------------------------------------------------------------------

def _dlq_conn():
    import psycopg2
    import psycopg2.extras
    url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _record_dead_letter(job_name: str, kwargs: dict, error: str, attempts: int):
    """Persist a poison job to dead_letter_jobs. Fire-and-forget - never raises."""
    try:
        conn = _dlq_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO dead_letter_jobs
                (job_name, kwargs, error, attempts, failed_at)
            VALUES (%s, %s, %s, %s, now())
        """, (job_name, json.dumps(kwargs), error[:4000], attempts))
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[worker] failed to write DLQ entry: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def extract_facts(ctx, *, ws: str, team_id: str = "shared"):
    """
    Pull LLM-extracted facts from any unextracted episodes in workspace `ws`.
    Idempotent - safe to re-enqueue; episodes already extracted are skipped.
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)
    ctx["trace_id"] = trace_id

    _wlog.info("job start", extra={"job": "extract_facts", "workspace_id": ws, "team_id": team_id})
    from brain.core import db, extract as _extract
    conn = db.connect()
    db.migrate(conn)
    try:
        n = _extract.run(conn, ws)
        _wlog.info("job done", extra={"job": "extract_facts", "workspace_id": ws,
                                      "team_id": team_id, "facts_extracted": n})
        return {"facts": n}
    except Exception as exc:
        _wlog.error("job error", extra={"job": "extract_facts", "workspace_id": ws,
                                        "team_id": team_id, "error": str(exc)})
        raise
    finally:
        conn.close()


async def ingest_event_job(ctx, *, ws: str, source: str, raw: dict, team_id: str = "shared"):
    """
    Webhook fast path: normalize and ingest a single raw event, then extract.
    Idempotent via content-hash dedup - duplicate deliveries are no-ops.
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)
    ctx["trace_id"] = trace_id

    _wlog.info("job start", extra={"job": "ingest_event_job", "workspace_id": ws, "source": source})
    from brain.engine import Brain
    brain = Brain(workspace_id=ws, team_id=team_id)
    try:
        result = brain.ingest_event(source, raw, team_id=team_id)
        _wlog.info("job done", extra={"job": "ingest_event_job", "workspace_id": ws,
                                      "source": source, "result": str(result)})
        return result
    except Exception as exc:
        _wlog.error("job error", extra={"job": "ingest_event_job", "workspace_id": ws,
                                        "source": source, "error": str(exc)})
        raise
    finally:
        brain.conn.close()


async def sync_source(ctx, *, ws: str, source: str, team_id: str = "shared",
                      trigger: str = "poll"):
    """
    Incremental connector poll for one source in workspace `ws`.
    Reads last_synced_at from connector_sync_state, passes it as watermark,
    then updates the watermark on success. Tracks failures with backoff.
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)
    ctx["trace_id"] = trace_id
    job_id = ctx.get("job_id", trace_id)

    _wlog.info("job start", extra={"job": "sync_source", "workspace_id": ws,
                                   "source": source, "team_id": team_id, "trigger": trigger})
    from brain.engine import Brain
    from brain.core import db as _db
    from brain.core.db_pg import (
        get_sync_state, set_sync_state,
        set_sync_state_running, set_sync_state_failed,
    )

    conn = _db.connect()
    _db.migrate(conn)
    state = get_sync_state(conn, ws, source)
    since = state["last_synced_at"]

    # Mark as running so concurrent cron ticks don't double-enqueue
    set_sync_state_running(conn, ws, source, job_id=job_id)
    conn.close()

    brain = Brain(workspace_id=ws, team_id=team_id)
    try:
        result = brain.sync(source, team_id=team_id, since=since)
        _wlog.info("job done", extra={"job": "sync_source", "workspace_id": ws,
                                      "source": source, "team_id": team_id,
                                      "trigger": trigger, "result": str(result)})
        # Update watermark only on success
        wconn = _db.connect()
        set_sync_state(wconn, ws, source, last_count=result.get("new_episodes", 0))
        wconn.close()
        return result
    except Exception as exc:
        error_str = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        _wlog.error("job error", extra={"job": "sync_source", "workspace_id": ws,
                                        "source": source, "team_id": team_id, "error": str(exc)})
        # Record failure and compute backoff
        fconn = _db.connect()
        set_sync_state_failed(fconn, ws, source, error=error_str[:2000])
        fconn.close()
        raise
    finally:
        brain.conn.close()


async def poll_all_workspaces(ctx):
    """
    Cron job: runs every BRAIN_POLL_INTERVAL_MINUTES (default 10).
    Discovers all workspaces with active facts and enqueues a sync_source
    job for each workspace + connector combination that is due.
    Skips: recently synced, currently running (not stuck), in backoff window.
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)

    from brain.core import db as _db
    from brain.core.db_pg import (
        list_active_workspaces, get_sync_state, reset_stuck_sync,
    )

    interval_min = int(os.getenv("BRAIN_POLL_INTERVAL_MINUTES", "10"))
    job_timeout_s = int(os.getenv("BRAIN_JOB_TIMEOUT_S", "300"))
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=interval_min)
    stuck_threshold = datetime.timedelta(seconds=job_timeout_s * 2)

    conn = _db.connect()
    _db.migrate(conn)
    workspaces = list_active_workspaces(conn)
    conn.close()

    if not workspaces:
        _wlog.info("poll_all: no active workspaces, skipping")
        return {"workspaces": 0, "jobs_enqueued": 0}

    enqueued = 0
    sources = _discover_sources()
    now = datetime.datetime.utcnow()

    try:
        import arq
        pool = await arq.create_pool(arq.connections.RedisSettings.from_dsn(config.REDIS_URL))
    except Exception as e:
        _wlog.error("poll_all: cannot connect to Redis, aborting: %s", e)
        return {"workspaces": 0, "jobs_enqueued": 0, "error": str(e)}

    try:
        for ws in workspaces:
            for source in sources:
                wconn = _db.connect()
                state = get_sync_state(wconn, ws, source)
                wconn.close()

                # Handle stuck running jobs
                if state["status"] == "running":
                    started_at = state.get("last_run_started_at")
                    if started_at and (now - started_at.replace(tzinfo=None)) > stuck_threshold:
                        rconn = _db.connect()
                        reset_stuck_sync(rconn, ws, source)
                        rconn.close()
                        _wlog.warning("poll_all: reset stuck sync", extra={
                            "workspace_id": ws, "source": source})
                    else:
                        continue  # let it finish

                # Skip if in backoff window
                if state["status"] == "failed":
                    locked_until = state.get("locked_until")
                    if locked_until:
                        lu = locked_until.replace(tzinfo=None) if hasattr(locked_until, "tzinfo") else locked_until
                        if lu and lu > now:
                            _wlog.info("poll_all: skip (backoff)", extra={
                                "workspace_id": ws, "source": source,
                                "locked_until": str(locked_until)})
                            continue

                # Skip if synced recently enough
                last = state.get("last_synced_at")
                if last:
                    last_naive = last.replace(tzinfo=None) if hasattr(last, "tzinfo") and last.tzinfo else last
                    if last_naive and last_naive > cutoff:
                        continue

                try:
                    await pool.enqueue_job("sync_source", ws=ws, source=source, team_id="shared",
                                           trigger="poll")
                    enqueued += 1
                    _wlog.info("poll_all: enqueued sync_source", extra={
                        "workspace_id": ws, "source": source})
                except Exception as e:
                    _wlog.error("poll_all: failed to enqueue %s/%s: %s", ws, source, e)
    finally:
        await pool.close()

    _wlog.info("poll_all done", extra={"workspaces": len(workspaces), "jobs_enqueued": enqueued})
    return {"workspaces": len(workspaces), "jobs_enqueued": enqueued}


async def reconcile_watermarks(ctx):
    """
    Cron job: gap-healing re-poll that runs every BRAIN_RECONCILE_INTERVAL_MINUTES.
    Re-polls from (last_synced_at - OVERLAP) for every workspace+source.
    Because ingest.py uses content-hash dedup, re-fetching already-seen items
    is always a no-op - only genuinely missed items produce new episodes.
    Does NOT advance the watermark (that is sync_source's job).
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)

    from brain.core import db as _db
    from brain.core.db_pg import list_active_workspaces, get_sync_state

    # Overlap window: 2x job_timeout x max_tries to cover worst-case timeout chain
    job_timeout_s = int(os.getenv("BRAIN_JOB_TIMEOUT_S", "300"))
    max_tries = int(os.getenv("BRAIN_JOB_MAX_RETRIES", "3"))
    overlap_s = job_timeout_s * max_tries * 2  # 1800s default
    overlap = datetime.timedelta(seconds=overlap_s)

    conn = _db.connect()
    _db.migrate(conn)
    workspaces = list_active_workspaces(conn)
    conn.close()

    if not workspaces:
        return {"workspaces": 0, "jobs_enqueued": 0}

    sources = _discover_sources()
    enqueued = 0

    try:
        import arq
        pool = await arq.create_pool(arq.connections.RedisSettings.from_dsn(config.REDIS_URL))
    except Exception as e:
        _wlog.error("reconcile: cannot connect to Redis: %s", e)
        return {"error": str(e)}

    try:
        for ws in workspaces:
            for source in sources:
                wconn = _db.connect()
                state = get_sync_state(wconn, ws, source)
                wconn.close()

                # Don't reconcile while a normal sync is in flight
                if state["status"] == "running":
                    continue

                last = state.get("last_synced_at")
                if last:
                    last_naive = last.replace(tzinfo=None) if hasattr(last, "tzinfo") and last.tzinfo else last
                    since = last_naive - overlap
                else:
                    since = None  # first-time: full backfill via reconcile

                try:
                    await pool.enqueue_job("sync_source", ws=ws, source=source,
                                           team_id="shared", trigger="reconcile",
                                           _job_id=f"reconcile:{ws}:{source}")
                    enqueued += 1
                    _wlog.info("reconcile: enqueued", extra={
                        "workspace_id": ws, "source": source,
                        "since": str(since)})
                except Exception as e:
                    _wlog.error("reconcile: enqueue failed %s/%s: %s", ws, source, e)
    finally:
        await pool.close()

    _wlog.info("reconcile done", extra={"workspaces": len(workspaces), "jobs_enqueued": enqueued})
    return {"workspaces": len(workspaces), "jobs_enqueued": enqueued}


def _discover_sources() -> list:
    """Return list of source names to poll. Composio covers all connected apps."""
    sources = ["composio"]
    if os.getenv("SLACK_BOT_TOKEN"):
        sources.append("slack")
    return sources


# ---------------------------------------------------------------------------
# arq on_job_abort hook - called when a job exceeds max_retries
# ---------------------------------------------------------------------------

async def on_job_abort(ctx, job_id: str, job_name: str, args, kwargs, error: Exception):
    """
    arq hook: called when a job is aborted (max_retries exceeded).
    Writes the job to dead_letter_jobs so operators can inspect and replay.
    """
    attempts = ctx.get("job_try", 0)
    trace_id = ctx.get("trace_id", "")
    if trace_id:
        _set_trace_id(trace_id)

    error_str = "".join(_tb.format_exception(type(error), error, error.__traceback__))
    _wlog.error(
        "job aborted - writing to dead-letter queue",
        extra={
            "job": job_name,
            "job_id": job_id,
            "attempts": attempts,
            "error": str(error),
        },
    )
    _record_dead_letter(
        job_name=job_name,
        kwargs=dict(kwargs),
        error=error_str,
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# Worker settings
# ---------------------------------------------------------------------------

def _build_cron_jobs():
    """Build the cron schedule. Returns empty list if arq is unavailable."""
    try:
        from arq import cron
        interval_min = int(os.getenv("BRAIN_POLL_INTERVAL_MINUTES", "10"))
        reconcile_min = int(os.getenv("BRAIN_RECONCILE_INTERVAL_MINUTES", "60"))
        return [
            cron(poll_all_workspaces, minute={i for i in range(0, 60, interval_min)}),
            cron(reconcile_watermarks, minute={i for i in range(0, 60, reconcile_min)}),
        ]
    except Exception as e:
        print(f"[worker] cron setup failed: {e}", file=sys.stderr)
        return []


class WorkerSettings:
    functions      = [extract_facts, sync_source, ingest_event_job,
                      poll_all_workspaces, reconcile_watermarks]
    cron_jobs      = _build_cron_jobs()
    on_job_abort   = on_job_abort
    redis_settings = None                      # set from env at import time
    max_jobs       = int(os.getenv("BRAIN_WORKER_CONCURRENCY", "4"))
    job_timeout    = int(os.getenv("BRAIN_JOB_TIMEOUT_S", "300"))  # 5 min
    max_tries      = int(os.getenv("BRAIN_JOB_MAX_RETRIES", "3"))
    keep_result    = 3600                      # keep job result in Redis for 1 hour
    queue_name     = "brain:queue"


# Resolve Redis settings at module-import time so the worker can be started
# by arq without needing any extra configuration.
try:
    from arq.connections import RedisSettings as _RS
    WorkerSettings.redis_settings = _RS.from_dsn(config.REDIS_URL)
except Exception as _e:
    print(f"[worker] could not resolve Redis settings: {_e}", file=sys.stderr)
