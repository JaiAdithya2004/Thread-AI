import uuid, datetime, json
from brain.core import llm, embed

SYSTEM = "You extract memory-worthy company facts from one item. JSON only."
PROMPT = """Source: {source}
Extract facts as JSON: {{"facts":[{{"subject":..,"predicate":..,"object":..,
"summary":..,"confidence":0-1,"supersedes":"<summary of fact this replaces or null>",
"visibility":"team or shared"}}]}}
Capture decisions, commitments, ownership, preferences, status changes.
Mark visibility as "shared" only if the fact is company-wide (applies to all teams).
Return {{"facts":[]}} if nothing worth remembering.

ITEM:
{body}
"""

def _now(): return datetime.datetime.utcnow().isoformat()

def run(conn, ws, incremental=False):
    # Process oldest-first so supersession works correctly:
    # the fact being superseded must exist before the superseder is saved.
    rows = conn.execute(
        "SELECT * FROM episodes WHERE workspace_id=? AND extracted=0"
        " ORDER BY occurred_at ASC NULLS LAST",
        (ws,)).fetchall()
    n = 0
    for ep in rows:
        out = llm.chat_json(SYSTEM, PROMPT.format(source=ep["source"],
                                                  body=ep["body"][:6000]))
        for f in out.get("facts", []):
            _save(conn, ws, f, ep); n += 1
        conn.execute("UPDATE episodes SET extracted=1 WHERE id=?", (ep["id"],))
        conn.commit()
    return n

def _save(conn, ws, f, ep):
    fid = str(uuid.uuid4())
    team_id = (ep["team_id"] if ep["team_id"] else "shared")
    visibility = f.get("visibility", "team")
    if visibility not in ("team", "shared"):
        visibility = "team"
    conn.execute(
        """INSERT INTO facts(id,workspace_id,subject,predicate,object,summary,
           episode_id,acl,confidence,introduced_at,source_kind,team_id,visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, ws, f.get("subject"), f.get("predicate"), f.get("object"),
         f.get("summary"), ep["id"], ep["acl"],
         float(f.get("confidence", 0.6)), _now(), "llm", team_id, visibility))
    _index(conn, fid, f.get("summary",""), f.get("subject",""), f.get("object",""))
    if f.get("supersedes"):
        _supersede(conn, ws, f.get("subject"), fid)
    conn.commit()

def _index(conn, fid, summary, subject, obj):
    conn.execute("INSERT INTO fact_fts(fact_id,summary,subject,object) VALUES (?,?,?,?)",
                 (fid, summary, subject, obj))
    conn.execute("INSERT INTO fact_vec(fact_id,embedding) VALUES (?,?)",
                 (fid, json.dumps(embed.embed(summary))))

def _supersede(conn, ws, subject, new_fid):
    old = conn.execute(
        "SELECT id FROM facts WHERE workspace_id=? AND subject=? "
        "AND invalidated_at IS NULL AND id!=? ORDER BY introduced_at DESC LIMIT 1",
        (ws, subject, new_fid)).fetchone()
    if old:
        conn.execute("UPDATE facts SET invalidated_at=? WHERE id=?", (_now(), old["id"]))
        conn.execute(
            "INSERT INTO fact_edges(workspace_id,src_fact,dst_fact,type) VALUES (?,?,?,?)",
            (ws, new_fid, old["id"], "supersedes"))

def record_human_fact(conn, ws, text, acl, team_id="shared", visibility="team"):
    fid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO facts(id,workspace_id,subject,predicate,object,summary,
           episode_id,acl,confidence,introduced_at,source_kind,team_id,visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, ws, "manual", "noted", "", text, None, ",".join(acl),
         1.0, _now(), "human", team_id, visibility))
    _index(conn, fid, text, "manual", "")
    conn.commit()
    return {"saved": fid, "summary": text}

def correct_fact(conn, ws, fact_id, correction, acl, team_id="shared"):
    """Hard /correct: invalidate the bad fact, add a trusted human fact, link it."""
    old = conn.execute(
        "SELECT team_id, visibility FROM facts WHERE id=?", (fact_id,)).fetchone()
    if old:
        team_id = old["team_id"] or team_id
        visibility = old["visibility"] or "team"
    else:
        visibility = "team"
    conn.execute("UPDATE facts SET invalidated_at=? WHERE id=? AND workspace_id=?",
                 (_now(), fact_id, ws))
    new = record_human_fact(conn, ws, correction, acl, team_id=team_id,
                            visibility=visibility)
    conn.execute(
        "INSERT INTO fact_edges(workspace_id,src_fact,dst_fact,type) VALUES (?,?,?,?)",
        (ws, new["saved"], fact_id, "supersedes"))
    conn.commit()
    return {"corrected": fact_id, "new_fact": new["saved"]}
