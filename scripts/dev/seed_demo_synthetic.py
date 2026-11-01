"""
seed_demo_synthetic.py - fill the public demo workspace with a fictional company.

The guest demo (workspace "ws_demo") is open to anyone, so it must never hold a
real person's data. This script replaces whatever is in it with the fictional
"Meridian Labs" knowledge base used by the benchmark (evals/corpus.yaml):
44 static facts plus facts that change over time (prices, owners, launch dates),
saved through the real reconcile pipeline so older versions are marked
superseded. Every fact is visibility="shared", so guests can read it.

Usage:
    python scripts/dev/seed_demo_synthetic.py --backup-dir DIR [--dry-run]

Only workspace "ws_demo" is touched. Before deleting, the existing rows are
written to DIR (keep it out of git: it may contain real personal data).
"""
import argparse
import datetime
import json
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml
from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

DEMO_WS = "ws_demo"
TEAM = "shared"


def _backup(conn, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    cur = conn.cursor()
    dump = {}
    for table, cols in (
        ("episodes", "id, source, title, body, occurred_at, visibility, owner_id"),
        ("facts", "id, subject, predicate, object, summary, introduced_at, invalidated_at, visibility, owner_id"),
    ):
        cur.execute(f"SELECT {cols} FROM {table} WHERE workspace_id=%s", (DEMO_WS,))
        dump[table] = [dict(r) for r in cur.fetchall()]
    cur.close()
    path = os.path.join(directory, f"ws_demo_backup_{datetime.date.today().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dump, fh, default=str, indent=1)
    return path


def _wipe(conn):
    cur = conn.cursor()
    for table in ("fact_edges", "facts", "episodes", "ledger"):
        cur.execute(f"DELETE FROM {table} WHERE workspace_id=%s", (DEMO_WS,))
        print(f"  deleted from {table}: {cur.rowcount}")
    conn.commit()
    cur.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(_ROOT, "evals", "corpus.yaml"), encoding="utf-8") as fh:
        corpus = yaml.safe_load(fh)
    base = corpus["facts"]
    chain_versions = [(c["id"], v) for c in corpus["chains"] for v in c["versions"]]
    print(f"corpus: {len(base)} static facts, {len(corpus['chains'])} changing chains "
          f"({len(chain_versions)} versions)")
    if args.dry_run:
        return 0

    from brain.core import db, embed, extract

    conn = db.connect()
    db.migrate(conn)
    print("backup ->", _backup(conn, args.backup_dir))

    # Embed first: if embeddings are unavailable, nothing has been deleted yet.
    base_embs = embed.embed_batch([f["summary"] for f in base])
    chain_embs = embed.embed_batch([v["text"] for _, v in chain_versions])
    for e in list(base_embs) + list(chain_embs):
        if not any(e):
            print("embedding unavailable (zero vector); aborting before any delete")
            return 1

    _wipe(conn)

    now = datetime.datetime.utcnow()
    for i, (f, emb) in enumerate(zip(base, base_embs)):
        db.save_fact(conn, DEMO_WS, {
            "id": str(uuid.uuid4()), "subject": f["subject"], "predicate": "states", "object": "",
            "summary": f["summary"], "episode_id": None, "acl": "everyone", "confidence": 0.9,
            "introduced_at": (now - datetime.timedelta(days=120, minutes=-i)).isoformat(),
            "source_kind": "llm", "team_id": TEAM, "visibility": "shared", "owner_id": None,
        }, emb)
    print(f"saved {len(base)} static facts")

    ep = {"id": None, "team_id": TEAM, "acl": "everyone", "source": "demo"}
    kept = 0
    for (cid, v), emb in zip(chain_versions, chain_embs):
        fact = {"subject": v["subject"], "predicate": "states", "object": v["value"],
                "summary": v["text"], "confidence": 0.9, "source_kind": "llm",
                "visibility": "shared"}
        if extract._reconcile_into(conn, DEMO_WS, fact, emb, ep):
            extract._save(conn, DEMO_WS, fact, ep, emb)
            kept += 1
    print(f"saved {kept} of {len(chain_versions)} changing-fact versions through reconcile")

    cur = conn.cursor()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE invalidated_at IS NOT NULL), "
                "count(*) FILTER (WHERE visibility<>'shared') FROM facts WHERE workspace_id=%s",
                (DEMO_WS,))
    row = cur.fetchone()
    total, stale, not_shared = list(row.values()) if isinstance(row, dict) else row
    cur.close()
    print(f"ws_demo now: {total} facts, {stale} superseded, {not_shared} not shared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
