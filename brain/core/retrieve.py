import json
from brain.core import db, embed, llm

def _vec(conn, query, n=30):
    q = json.dumps(embed.embed(query))
    return [r["fact_id"] for r in conn.execute(
        "SELECT fact_id FROM fact_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (q, n)).fetchall()]

def _fts(conn, query, n=30):
    safe = " OR ".join(w for w in query.split() if w.isalnum())
    if not safe: return []
    return [r["fact_id"] for r in conn.execute(
        "SELECT fact_id FROM fact_fts WHERE fact_fts MATCH ? LIMIT ?",
        (safe, n)).fetchall()]

def _rrf(*lists, k=60):
    s = {}
    for lst in lists:
        for rank, fid in enumerate(lst):
            s[fid] = s.get(fid, 0) + 1.0/(k+rank)
    return sorted(s, key=s.get, reverse=True)

def _expand(query):
    try:
        r = llm.chat_json('Return JSON {"terms":[...]} of related search terms.',
                          f"Query: {query}")
        return query + " " + " ".join(r.get("terms", [])[:5])
    except Exception:
        return query

def _allowed(acl_str, principals):
    acl = (acl_str or "everyone").split(",")
    return "everyone" in acl or any(p in acl for p in principals)

def _team_visible(row, team_id, cross_team):
    """Fact is visible when visibility='shared', team matches, or cross_team is True."""
    if cross_team:
        return True
    vis = row["visibility"] or "team"
    if vis == "shared":
        return True
    return (row["team_id"] or "shared") == (team_id or "shared")

def run(conn, ws, query, principals, caller, surface,
        team_id=None, include_invalid=False, cross_team=False, top=12):
    fused = _rrf(_vec(conn, query), _fts(conn, _expand(query)))
    facts = []
    for fid in fused:
        row = conn.execute(
            "SELECT * FROM facts WHERE id=? AND workspace_id=?", (fid, ws)).fetchone()
        if not row: continue
        if not _allowed(row["acl"], principals):     # PERMISSION FILTER before return
            continue
        if not _team_visible(row, team_id, cross_team):
            continue
        if row["invalidated_at"] and not include_invalid:
            continue
        facts.append(dict(row))
    facts.sort(key=lambda f: (f["invalidated_at"] is not None,
                              fused.index(f["id"])))
    facts = facts[:top]
    db.log(conn, ws, caller, query, surface, [f["id"] for f in facts],
           "\n".join(f["summary"] for f in facts), team_id=team_id)
    return facts

def _cite(f):
    tag = "stale" if f["invalidated_at"] else "valid"
    return {"summary": f["summary"], "status": tag,
            "confidence": round(f["confidence"] or 0, 2),
            "source": f["source_kind"], "fact_id": f["id"],
            "team": f.get("team_id"), "visibility": f.get("visibility"),
            "episode_id": f.get("episode_id")}

def format_answer(query, facts):
    if not facts or (facts[0]["confidence"] or 0) < 0.35:
        return {"answer": "I am not certain. Closest facts:",
                "facts": [_cite(f) for f in facts[:3]]}
    return {"answer": "From company memory:", "facts": [_cite(f) for f in facts]}

def format_history(subject, facts):
    facts.sort(key=lambda f: f["introduced_at"] or "")
    return {"subject": subject, "timeline": [_cite(f) for f in facts]}
