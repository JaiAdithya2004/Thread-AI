"""
arq async worker — runs background jobs enqueued by Brain.

Start with:
    arq brain.worker.WorkerSettings

Jobs:
    extract_facts(ws, team_id)   — pull facts from unextracted episodes
    sync_source(ws, source, team_id) — incremental connector poll

Dead-letter:
    Any job that exceeds max_retries lands in the `dead_letter_jobs`
    Postgres table instead of being retried forever. The API exposes
    dead-letter state at GET /api/admin/dead-letter.
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
    import psycopg2, psycopg2.extras
    url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _record_dead_letter(job_name: str, kwargs: dict, error: str, attempts: int):
    """Persist a poison job to dead_letter_jobs. Fire-and-forget — never raises."""
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
    Idempotent — safe to re-enqueue; episodes already extracted are skipped.
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


async def sync_source(ctx, *, ws: str, source: str, team_id: str = "shared"):
    """
    Incremental connector poll for one source in workspace `ws`.
    Connector failures are logged but do not crash the worker process.
    """
    trace_id = str(_uuid.uuid4())
    _set_trace_id(trace_id)
    ctx["trace_id"] = trace_id

    _wlog.info("job start", extra={"job": "sync_source", "workspace_id": ws,
                                   "source": source, "team_id": team_id})
    from brain.engine import Brain
    brain = Brain(workspace_id=ws, team_id=team_id)
    try:
        result = brain.sync(source, team_id=team_id)
        _wlog.info("job done", extra={"job": "sync_source", "workspace_id": ws,
                                      "source": source, "team_id": team_id, "result": str(result)})
        return result
    except Exception as exc:
        _wlog.error("job error", extra={"job": "sync_source", "workspace_id": ws,
                                        "source": source, "team_id": team_id, "error": str(exc)})
        raise  # let arq count the retry
    finally:
        brain.conn.close()


# ---------------------------------------------------------------------------
# arq on_job_abort hook — called when a job exceeds max_retries
# ---------------------------------------------------------------------------

async def on_job_abort(ctx, job_id: str, job_name: str, args, kwargs, error: Exception):
    """
    arq hook: called when a job is aborted (max_retries exceeded).
    We write the job to the dead_letter_jobs table so operators can inspect
    and replay or discard it.
    """
    attempts = ctx.get("job_try", 0)
    # Restore trace_id from ctx if the job set one
    trace_id = ctx.get("trace_id", "")
    if trace_id:
        _set_trace_id(trace_id)

    error_str = "".join(_tb.format_exception(type(error), error, error.__traceback__))
    _wlog.error(
        "job aborted — writing to dead-letter queue",
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

class WorkerSettings:
    functions      = [extract_facts, sync_source]
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
