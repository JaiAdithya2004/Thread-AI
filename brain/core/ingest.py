import hashlib, uuid
from brain.config import WORTHY_MIN_LEN

def _hash(s): return hashlib.sha256((s or "").encode()).hexdigest()

def _worthy(ep):
    """Cheap memory-worthiness gate: drop noise before extraction.
    Replace with an LLM classifier for promo/social filtering."""
    body = (ep.get("body") or "").strip()
    if len(body) < WORTHY_MIN_LEN:
        return False
    noise = ("unsubscribe", "view in browser", "shipping confirmation")
    low = body.lower()
    return not any(n in low for n in noise)

def run(conn, ws, episodes, team_id="shared"):
    """episodes: iterable of normalized dicts from a connector.
    team_id: fallback team when episode doesn't carry one explicitly.
    """
    before = conn.execute("SELECT COUNT(*) FROM episodes WHERE workspace_id=?",
                          (ws,)).fetchone()[0]
    for ep in episodes:
        if not _worthy(ep):
            continue
        h = _hash(f"{ep['source']}:{ep['external_id']}:{ep.get('body','')}")
        conn.execute(
            """INSERT OR IGNORE INTO episodes
               (id,workspace_id,source,external_id,title,body,author,url,
                occurred_at,content_hash,acl,team_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), ws, ep["source"], ep["external_id"],
             ep.get("title",""), ep.get("body",""), ep.get("author",""),
             ep.get("url",""), ep.get("occurred_at",""), h,
             ",".join(ep.get("acl", ["everyone"])),
             ep.get("team_id", team_id)))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM episodes WHERE workspace_id=?",
                         (ws,)).fetchone()[0]
    return after - before
