"""
One-shot connector sync. Runs a single incremental pass and exits.

Why this exists
---------------
The arq worker (brain/worker.py) needs Redis and a long-running process. That
is the right design for webhook ingestion and retry backoff, but it needs a
host that keeps a process alive — which rules out serverless platforms.

This script does the same incremental sync in a single pass, with no queue and
no daemon, so any scheduler can drive it: GitHub Actions, a Vercel cron, a
systemd timer, or a manual run. It reuses the same watermark in
connector_sync_state, so it interoperates with the worker rather than
competing with it — whichever runs last simply advances the watermark.

Usage:
    python scripts/sync_once.py                    # sync every workspace
    python scripts/sync_once.py --workspace ws_demo
    python scripts/sync_once.py --dry-run          # report what would sync

Exit codes:
    0  all workspaces synced (or nothing to do)
    1  one or more workspaces failed
"""
import argparse
import os
import sys
import time
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_env():
    """Load .env when present. Absent in CI, where secrets are already exported."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass


def _discover_workspaces(conn, explicit=None):
    """Workspaces to sync: the one given, else every workspace holding data."""
    if explicit:
        return [explicit]
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT workspace_id FROM episodes ORDER BY workspace_id")
    rows = [r["workspace_id"] for r in cur.fetchall()]
    cur.close()
    return rows


def sync_workspace(ws: str, source: str = "composio", team_id: str = "shared") -> dict:
    """Incremental sync for one workspace. Mirrors worker.sync_source without arq."""
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
    set_sync_state_running(conn, ws, source, job_id="sync_once")
    conn.close()

    brain = Brain(workspace_id=ws, team_id=team_id)
    try:
        result = brain.sync(source, team_id=team_id, since=since)
        wconn = _db.connect()
        # Watermark advances only on success, so a failed pass refetches its
        # window on the next run instead of silently skipping it.
        set_sync_state(wconn, ws, source, last_count=result.get("new_episodes", 0))
        wconn.close()
        return result
    except Exception as exc:
        err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        fconn = _db.connect()
        set_sync_state_failed(fconn, ws, source, error=err[:2000])
        fconn.close()
        raise
    finally:
        try:
            brain.conn.close()
        except Exception:
            pass


def main() -> int:
    p = argparse.ArgumentParser(description="One-shot connector sync")
    p.add_argument("--workspace", default=os.getenv("BRAIN_WORKSPACE"),
                   help="Workspace to sync. Defaults to BRAIN_WORKSPACE, "
                        "or every workspace holding episodes.")
    p.add_argument("--source", default="composio")
    p.add_argument("--team", default="shared")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be synced without calling the provider.")
    args = p.parse_args()

    _load_env()
    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1

    from brain.core import db as _db
    conn = _db.connect()
    workspaces = _discover_workspaces(conn, args.workspace)
    conn.close()

    if not workspaces:
        print("no workspaces to sync")
        return 0

    print(f"syncing {len(workspaces)} workspace(s) from '{args.source}'")
    failures = 0
    for ws in workspaces:
        t0 = time.perf_counter()
        if args.dry_run:
            print(f"  [dry-run] would sync {ws}")
            continue
        try:
            r = sync_workspace(ws, source=args.source, team_id=args.team)
            print(f"  OK   {ws}: +{r.get('new_episodes', 0)} episodes, "
                  f"+{r.get('new_facts', 0)} facts  ({time.perf_counter()-t0:.1f}s)")
        except Exception as exc:
            failures += 1
            # Keep going: one bad workspace must not block the rest.
            print(f"  FAIL {ws}: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)

    if failures:
        print(f"\n{failures}/{len(workspaces)} workspace(s) failed", file=sys.stderr)
        return 1
    print("\nall workspaces synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
