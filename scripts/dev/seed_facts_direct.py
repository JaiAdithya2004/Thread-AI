"""
seed_facts_direct.py — Insert pre-written facts from episode content directly,
bypassing LLM extraction. Uses Gemini embeddings only (different quota from generation).

Run when: Gemini generation quota is exhausted but embeddings still work.
Usage: python scripts/dev/seed_facts_direct.py
"""

import sys, os, uuid, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

DEMO_WS = "ws_demo"

def _now():
    return datetime.datetime.utcnow().isoformat()

def _make_facts_from_episode(ep: dict) -> list:
    """Generate simple declarative facts from episode content."""
    facts = []
    source = ep.get("source", "")
    title = ep.get("title", "")
    body = ep.get("body", "")
    team_id = ep.get("team_id", "shared")
    ep_id = ep.get("id", "")
    acl = ep.get("acl", "everyone")

    if "github" in source:
        repo_name = title.split("/")[-1] if "/" in title else title
        desc = body.split("\n\n", 1)[-1][:200] if "\n\n" in body else ""
        summary = f"{title} is a GitHub repository."
        if desc and len(desc) > 10:
            summary += f" {desc.strip()[:150]}"
        facts.append({
            "subject": repo_name,
            "predicate": "is",
            "object": "a GitHub repository",
            "summary": summary,
            "acl": acl,
            "team_id": team_id,
            "episode_id": ep_id,
            "confidence": 0.9,
            "source_kind": "integration",
        })

    elif "gmail" in source:
        subject_line = title[:80]
        sender = ""
        for line in body.split("\n"):
            if line.startswith("From:"):
                sender = line[5:].strip()[:60]
                break
        summary = f'Email "{subject_line}"'
        if sender:
            summary += f" from {sender}"
        facts.append({
            "subject": subject_line,
            "predicate": "received",
            "object": sender or "unknown sender",
            "summary": summary,
            "acl": acl,
            "team_id": team_id,
            "episode_id": ep_id,
            "confidence": 0.85,
            "source_kind": "integration",
        })

    elif "googlecalendar" in source:
        cal_name = title
        summary = f"Google Calendar: {cal_name}"
        if "description" in body:
            desc = body.split("\n", 1)[-1][:100] if "\n" in body else ""
            if desc:
                summary += f" — {desc}"
        facts.append({
            "subject": cal_name,
            "predicate": "is",
            "object": "a Google Calendar",
            "summary": summary,
            "acl": acl,
            "team_id": team_id,
            "episode_id": ep_id,
            "confidence": 0.85,
            "source_kind": "integration",
        })

    return facts


def main():
    from brain.core import db_pg as pg
    from brain.core import embed as emb

    conn = pg.connect()
    cur = conn.cursor()

    # Fetch all episodes in ws_demo
    cur.execute(
        "SELECT id, title, body, acl, team_id, source, external_id "
        "FROM episodes WHERE workspace_id=%s ORDER BY source, title",
        (DEMO_WS,)
    )
    episodes = cur.fetchall()
    cur.close()
    print(f"Found {len(episodes)} episodes in {DEMO_WS}")

    # Check existing facts
    cur2 = conn.cursor()
    cur2.execute("SELECT count(*) as n FROM facts WHERE workspace_id=%s", (DEMO_WS,))
    existing = cur2.fetchone()["n"]
    cur2.close()
    if existing > 0:
        print(f"{existing} facts already exist — run with --reset to clear them first.")

    all_facts = []
    for ep in episodes:
        facts = _make_facts_from_episode(dict(ep))
        all_facts.extend(facts)

    print(f"Generated {len(all_facts)} facts from episodes")
    print("Computing embeddings (this uses Gemini embed, not generation)...")

    summaries = [f["summary"] for f in all_facts]

    # Embed in batches
    vectors = emb.embed_batch(summaries)
    if not vectors or len(vectors) != len(all_facts):
        print(f"ERROR: Got {len(vectors)} vectors for {len(all_facts)} facts")
        conn.close()
        sys.exit(1)

    print(f"Embeddings computed. Inserting {len(all_facts)} facts...")

    inserted = 0
    cur3 = conn.cursor()
    for fact, vec in zip(all_facts, vectors):
        fid = str(uuid.uuid4())
        emb_str = "[" + ",".join(str(x) for x in vec) + "]"
        cur3.execute("""
            INSERT INTO facts
                (id, workspace_id, team_id, subject, predicate, object,
                 summary, episode_id, acl, confidence, introduced_at,
                 source_kind, fact_vec_768, visibility)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)
            ON CONFLICT DO NOTHING
        """, (
            fid, DEMO_WS, fact["team_id"],
            fact["subject"][:200], fact["predicate"][:200], fact.get("object", "")[:200],
            fact["summary"][:500],
            fact["episode_id"],
            fact["acl"],
            fact["confidence"],
            _now(),
            fact["source_kind"],
            emb_str,
            "shared",
        ))
        inserted += 1

    conn.commit()
    cur3.close()
    conn.close()

    print(f"\nDone! Inserted {inserted} facts into {DEMO_WS}.")
    print("The demo workspace is now searchable.")


if __name__ == "__main__":
    main()
