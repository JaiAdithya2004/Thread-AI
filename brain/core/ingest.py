import hashlib, uuid
from brain.config import WORTHY_MIN_LEN
from brain.core import db as _db


def _hash(s):
    return hashlib.sha256((s or "").encode()).hexdigest()


_NOISE = (
    # email noise
    "unsubscribe", "view in browser", "shipping confirmation",
    # slack admin / bot events
    "has renamed the channel",
    "has joined the channel",
    "has left the channel",
    "set the channel topic",
    "set the channel description",
    "added an integration",
    "removed an integration",
)

_ACK_PATTERNS = (
    # pure acknowledgement messages — no information content
    "sounds good", "will do", "got it", "lgtm", "noted",
    "👍", "✅", "+1", "agreed", "sure", "ok", "okay",
)


def _worthy(ep):
    body = (ep.get("body") or "").strip()
    if len(body) < WORTHY_MIN_LEN:
        return False
    low = body.lower()
    if any(n in low for n in _NOISE):
        return False
    if len(body) <= 40 and any(low == p or low.strip(".,!") == p for p in _ACK_PATTERNS):
        return False
    return True


def _ph():
    """Return the placeholder char for the active backend."""
    return "%s" if _db.BACKEND == "postgres" else "?"


def run(conn, ws, episodes, team_id="shared"):
    ph = _ph()
    inserted = 0

    for ep in episodes:
        if not _worthy(ep):
            continue
        body = ep.get("body", "")
        h = _hash(body)

        if _db.BACKEND == "postgres":
            cur = conn.cursor()
            cur.execute(
                f"SELECT 1 FROM episodes WHERE workspace_id={ph} AND content_hash={ph} LIMIT 1",
                (ws, h),
            )
            existing = cur.fetchone()
            cur.close()
        else:
            existing = conn.execute(
                "SELECT 1 FROM episodes WHERE workspace_id=? AND content_hash=? LIMIT 1",
                (ws, h),
            ).fetchone()

        if existing:
            continue

        ep_id = str(uuid.uuid4())
        acl = ",".join(ep.get("acl", ["everyone"]))
        params = (
            ep_id, ws, ep["source"], ep.get("external_id", ep_id),
            ep.get("title", ""), body, ep.get("author", ""),
            ep.get("url", ""), ep.get("occurred_at", ""), h,
            acl, ep.get("team_id", team_id),
        )

        if _db.BACKEND == "postgres":
            cur = conn.cursor()
            cur.execute(
                f"""INSERT INTO episodes
                    (id,workspace_id,source,external_id,title,body,author,url,
                     occurred_at,content_hash,acl,team_id)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT (workspace_id,content_hash) DO NOTHING""",
                params,
            )
            rows_affected = cur.rowcount
            cur.close()
            conn.commit()
            if rows_affected:
                inserted += 1
        else:
            conn.execute(
                """INSERT OR IGNORE INTO episodes
                   (id,workspace_id,source,external_id,title,body,author,url,
                    occurred_at,content_hash,acl,team_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                params,
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1

    if _db.BACKEND != "postgres":
        conn.commit()

    return inserted
