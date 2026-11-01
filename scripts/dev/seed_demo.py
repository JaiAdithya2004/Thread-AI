"""
seed_demo.py — Seed the demo workspace with real data from connected integrations.

Usage:
    python scripts/dev/seed_demo.py [--dry-run] [--reset]

Options:
    --dry-run   Print episodes that would be ingested; skip DB writes and LLM calls.
    --reset     Drop all demo data first, then re-seed. (Safe to run repeatedly.)

What it does:
    1. Creates a demo workspace (workspace_id = "ws_demo") — completely isolated
       from any existing production workspace.
    2. Provisions two departments: Engineering, Personal.
    3. Fetches the last 3 months of data from:
         • Gmail          (auth config: ac_FNW7U3l6Mx_y)
         • Google Calendar (auth config: ac_OU8_SBN1R0Xo)
         • GitHub         (auth config: ac_9ZdRZsEeF6OC)
    4. Runs ingest → extract (LLM fact extraction) → embeddings (Gemini 768-dim).
    5. Idempotent: existing episodes/facts are skipped via content-hash dedup.

ISOLATION GUARANTEE:
    All writes use workspace_id="ws_demo". The production workspace is never touched.
"""

import sys, os, datetime, argparse

# ── ensure project root is on sys.path ────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

# ── constants ─────────────────────────────────────────────────────────────────

DEMO_WS          = "ws_demo"
DEMO_USER_ID     = "demo_user"          # entity_id used for Composio calls
DEMO_PRINCIPALS  = [DEMO_USER_ID, "everyone"]

# Real connected account IDs from Composio (fetched via list_connected_apps()).
# entity_id is the owner of these connected accounts.
DEMO_ENTITY_ID = "pg-test-396b7d6a-d91b-4f0d-8378-5481d776509b"
DEMO_CONNECTIONS = {
    "gmail":          "ca_ImkN5ylpuGC3",
    "googlecalendar": "ca_qbo_e7xLbXTf",
    "github":         "ca_y5nYpst3_Uke",
}

# Keep ingestion fast — only last 3 months, capped per source
THREE_MONTHS_AGO = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y/%m/%d")

# Actions to call per app with capped limits
SEED_ACTIONS = {
    "gmail": [
        ("GMAIL_FETCH_EMAILS", {
            "max_results": 50,
            "query": f"category:primary after:{THREE_MONTHS_AGO}",
        }),
    ],
    "googlecalendar": [
        ("GOOGLECALENDAR_LIST_CALENDARS", {}),
        ("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {}),
    ],
    "github": [
        ("GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", {"per_page": 30}),
        ("GITHUB_LIST_ISSUES_BY_REPO", {"per_page": 30}),
    ],
}

# Department → app mapping (episodes tagged to these teams)
DEPT_MAP = {
    "engineering": ["github"],
    "personal":    ["gmail", "googlecalendar"],
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _print(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _app_to_team(app: str) -> str:
    for team, apps in DEPT_MAP.items():
        if app in apps:
            return team
    return "shared"


# ── calendar normalizer (not in composio.py yet) ──────────────────────────────

def _normalize_calendar(action, raw_result) -> list:
    """Convert Google Calendar results → Episode dicts."""
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat()
    episodes = []
    data = raw_result.get("response") or raw_result.get("data") or raw_result

    if action == "GOOGLECALENDAR_LIST_CALENDARS":
        cals = data if isinstance(data, list) else data.get("calendars", data.get("items", []))
        for cal in cals:
            title   = cal.get("summary") or cal.get("name") or "(calendar)"
            desc    = cal.get("description") or ""
            cal_id  = cal.get("id", "")
            if not title or title == "(calendar)":
                continue
            body = f"Google Calendar: {title}"
            if desc:
                body += f"\n{desc}"
            episodes.append({
                "source":      "composio:googlecalendar",
                "external_id": f"gcal:calendar:{cal_id}",
                "title":       title,
                "body":        body,
                "author":      "google-calendar",
                "url":         "",
                "occurred_at": now,
                "acl":         "everyone",
            })

    elif action == "GOOGLECALENDAR_GET_CURRENT_DATE_TIME":
        # Minimal signal — just confirms Calendar is connected
        dt_val = data.get("dateTime") or data.get("current_datetime") or str(data)
        episodes.append({
            "source":      "composio:googlecalendar",
            "external_id": "gcal:current_datetime",
            "title":       "Current date/time",
            "body":        f"Current date/time via Google Calendar API: {dt_val}",
            "author":      "google-calendar",
            "url":         "",
            "occurred_at": now,
            "acl":         "everyone",
        })

    return episodes


# ── fetch from Composio using the provided entity IDs ─────────────────────────

def _fetch_app(app: str, connected_account_id: str, dry_run: bool) -> list:
    """Call Composio actions for one app and return normalized episodes."""
    from brain.core.connectors.composio import execute_action, _normalize_result

    actions = SEED_ACTIONS.get(app, [])
    all_episodes = []

    for action_slug, args in actions:
        _print(f"  > {app}/{action_slug} (account={connected_account_id})")
        if dry_run:
            _print(f"    [dry-run] would call execute_action('{action_slug}', entity_id='{DEMO_ENTITY_ID}', connected_account_id='{connected_account_id}')")
            continue
        try:
            result = execute_action(action_slug, args,
                                    entity_id=DEMO_ENTITY_ID,
                                    connected_account_id=connected_account_id)
            if app == "googlecalendar":
                eps = _normalize_calendar(action_slug, result)
            else:
                eps = list(_normalize_result(app, action_slug, result))
            _print(f"    got {len(eps)} episode(s)")
            all_episodes.extend(eps)
        except Exception as e:
            _print(f"    ERROR: {e}")

    return all_episodes


# ── reset demo workspace ───────────────────────────────────────────────────────

def _reset_demo(conn):
    """Delete all demo workspace data from episodes, facts, fact_edges, ledger, teams."""
    _print("Resetting demo workspace data...")
    cur = conn.cursor()
    # Must delete facts first (FK → episodes)
    cur.execute("DELETE FROM facts WHERE workspace_id = %s", (DEMO_WS,))
    cur.execute("DELETE FROM fact_edges WHERE workspace_id = %s", (DEMO_WS,))
    cur.execute("DELETE FROM ledger WHERE workspace_id = %s", (DEMO_WS,))
    cur.execute("DELETE FROM episodes WHERE workspace_id = %s", (DEMO_WS,))
    cur.execute("DELETE FROM teams WHERE workspace_id = %s", (DEMO_WS,))
    conn.commit()
    cur.close()
    _print("Demo data cleared.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Seed the Thread demo workspace.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be ingested without writing to DB or calling LLMs.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete existing demo data before seeding.")
    args = parser.parse_args()

    dry_run = args.dry_run

    # Validate required env vars
    if not os.getenv("COMPOSIO_API_KEY"):
        _print("ERROR: COMPOSIO_API_KEY is not set. Add it to .env and retry.")
        sys.exit(1)
    if not os.getenv("DATABASE_URL"):
        _print("ERROR: DATABASE_URL is not set. Add it to .env and retry.")
        sys.exit(1)

    _print(f"=== Thread Demo Seed {'(DRY RUN) ' if dry_run else ''}===")
    _print(f"Demo workspace: {DEMO_WS}")
    _print(f"Connections: {list(DEMO_CONNECTIONS.keys())}")
    _print(f"Date window: last 3 months (after {THREE_MONTHS_AGO})")

    # ── DB setup ──────────────────────────────────────────────────────────────
    from brain.core import db_pg as pg

    conn = pg.connect()
    pg.migrate(conn)

    if args.reset:
        _reset_demo(conn)

    # ── Provision demo teams ──────────────────────────────────────────────────
    _print("\nProvisioning departments...")
    from brain.core.db_pg import ensure_team

    departments = [
        ("engineering", "Engineering", "Software engineering projects and repos"),
        ("personal",    "Personal",    "Personal emails and calendar"),
    ]
    for team_id, name, desc in departments:
        if not dry_run:
            ensure_team(conn, DEMO_WS, team_id, name, desc)
        _print(f"  - {name} ({team_id})")

    # ── Fetch episodes from each connected app ────────────────────────────────
    _print("\nFetching data from connected integrations...")

    all_episodes = []
    for app, entity_id in DEMO_CONNECTIONS.items():
        _print(f"\n[{app.upper()}]  entity_id={entity_id}")
        team_id = _app_to_team(app)
        episodes = _fetch_app(app, entity_id, dry_run)
        for ep in episodes:
            ep.setdefault("team_id", team_id)
            # ACL: demo data is visible to everyone (guests use "everyone" principal)
            ep["acl"] = ["everyone"]
        all_episodes.extend(episodes)
        _print(f"  Subtotal: {len(episodes)} episode(s) from {app}")

    _print(f"\nTotal episodes fetched: {len(all_episodes)}")

    if dry_run:
        _print("\n[dry-run] Sample episodes (first 3):")
        for ep in all_episodes[:3]:
            _print(f"  [{ep.get('source')}] {ep.get('title', '')[:80]}")
            _print(f"    body[:120]: {ep.get('body','')[:120]}")
        _print("\n[dry-run] No DB writes performed. Re-run without --dry-run to seed.")
        conn.close()
        return

    if not all_episodes:
        _print("\nNo episodes fetched — check that the auth config IDs are still active in Composio.")
        conn.close()
        return

    # ── Ingest episodes → episodes table ─────────────────────────────────────
    _print("\nIngesting episodes...")
    from brain.core import ingest

    # Group by team so ingest.run() tags correctly
    from collections import defaultdict
    by_team = defaultdict(list)
    for ep in all_episodes:
        by_team[ep.get("team_id", "shared")].append(ep)

    total_ingested = 0
    for team_id, eps in by_team.items():
        n = ingest.run(conn, DEMO_WS, eps, team_id=team_id)
        total_ingested += n
        _print(f"  [{team_id}] {n}/{len(eps)} new episodes saved")

    _print(f"Total new episodes stored: {total_ingested}")

    if total_ingested == 0:
        _print("All episodes already existed (idempotent). Skipping extraction.")
        conn.close()
        return

    # ── Extract facts → LLM + embeddings ─────────────────────────────────────
    _print("\nExtracting facts (LLM + Gemini embeddings)...")
    _print("  This may take 1–3 minutes depending on episode count.")

    from brain.core import extract

    # extract.run() processes at most 20 episodes per call — loop until dry.
    total_facts = 0
    rounds = 0
    try:
        while True:
            n = extract.run(conn, DEMO_WS, limit=50)
            total_facts += n
            rounds += 1
            _print(f"  Round {rounds}: {n} fact(s) extracted")
            if n == 0:
                break
            if rounds > 20:
                _print("  (Stopping after 20 rounds — re-run to continue.)")
                break
    except Exception as e:
        _print(f"  ERROR during extraction: {e}")
        _print("  Episodes are saved — re-run seed_demo.py (without --reset) to retry extraction.")
        conn.close()
        sys.exit(1)
    n_facts = total_facts

    # ── Done ─────────────────────────────────────────────────────────────────
    conn.close()
    _print(f"\n=== Seed complete ===")
    _print(f"Workspace:  {DEMO_WS}")
    _print(f"Episodes:   {total_ingested} new")
    _print(f"Facts:      {n_facts} extracted")
    _print("")
    _print("To query the demo workspace, set these env vars:")
    _print(f"  BRAIN_WORKSPACE={DEMO_WS}")
    _print(f"  BRAIN_USER={DEMO_USER_ID}")
    _print(f"  BRAIN_PRINCIPALS={DEMO_USER_ID},everyone")
    _print("Then run:  python -m brain.wrappers.cli ask \"<your question>\"")
    _print("")
    _print("Or query directly via the Brain engine:")
    _print("  from brain.engine import Brain")
    _print(f"  b = Brain(workspace_id='{DEMO_WS}', caller='{DEMO_USER_ID}', principals=['{DEMO_USER_ID}','everyone'])")
    _print("  print(b.search('What GitHub repos do I have?'))")


if __name__ == "__main__":
    main()
