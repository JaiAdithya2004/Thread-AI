import hashlib, re, uuid
from brain.config import WORTHY_MIN_LEN


def _hash(s):
    return hashlib.sha256((s or "").encode()).hexdigest()


_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?((previous|prior|above|earlier|your)\s+)?instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?((previous|prior|above|earlier|your)\s+)?instructions?", re.I),
    re.compile(r"forget\s+(all\s+)?((previous|prior|above|earlier|your)\s+)?instructions?", re.I),
    re.compile(r"<\s*/?\s*(system|user|assistant|prompt)\s*>", re.I),
    re.compile(r"\[SYSTEM\]", re.I),
    re.compile(r"you\s+are\s+now\s+(a\s+|an?\s+)?(?!sure|certain|able|ready)", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
    re.compile(r"(act|behave|respond)\s+as\s+(if\s+you\s+(are|were)|a\s+)", re.I),
]


def _sanitize(body: str) -> str:
    for pat in _INJECTION_PATTERNS:
        body = pat.sub("[filtered]", body)
    return body


_NOISE = (
    "unsubscribe", "view in browser", "shipping confirmation",
    "has renamed the channel",
    "has joined the channel",
    "has left the channel",
    "set the channel topic",
    "set the channel description",
    "added an integration",
    "removed an integration",
)

_ACK_PATTERNS = (
    "sounds good", "will do", "got it", "lgtm", "noted",
    "\U0001f44d", "✅", "+1", "agreed", "sure", "ok", "okay",
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


def _find_prior_episode(conn, ws: str, external_id: str) -> dict | None:
    """Look up the most recent episode for this external_id in this workspace.
    Returns {id, content_hash} or None."""
    if not external_id:
        return None
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content_hash FROM episodes
           WHERE workspace_id=%s AND external_id=%s
           ORDER BY occurred_at DESC, id DESC LIMIT 1""",
        (ws, external_id),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def run(conn, ws, episodes, team_id="shared", trigger="poll"):
    inserted = 0

    for ep in episodes:
        if not _worthy(ep):
            continue
        body = _sanitize(ep.get("body", ""))
        h = _hash(body)
        ext_id = ep.get("external_id", "")

        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM episodes WHERE workspace_id=%s AND content_hash=%s LIMIT 1",
            (ws, h),
        )
        existing = cur.fetchone()
        cur.close()

        if existing:
            continue

        # Check if this external_id was seen before with different content (updated item)
        supersedes_id = None
        if ext_id:
            prior = _find_prior_episode(conn, ws, ext_id)
            if prior and prior["content_hash"] != h:
                supersedes_id = prior["id"]

        ep_id = str(uuid.uuid4())
        _acl = ep.get("acl", ["everyone"])
        acl = _acl if isinstance(_acl, str) else ",".join(_acl)
        params = (
            ep_id, ws, ep["source"], ext_id or ep_id,
            ep.get("title", ""), body, ep.get("author", ""),
            ep.get("url", ""), ep.get("occurred_at", ""), h,
            acl, ep.get("team_id", team_id), supersedes_id, trigger,
        )

        cur = conn.cursor()
        cur.execute(
            """INSERT INTO episodes
               (id,workspace_id,source,external_id,title,body,author,url,
                occurred_at,content_hash,acl,team_id,supersedes_episode_id,ingest_trigger)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (workspace_id,content_hash) DO NOTHING""",
            params,
        )
        if cur.rowcount:
            inserted += 1
        cur.close()
        conn.commit()

    return inserted
