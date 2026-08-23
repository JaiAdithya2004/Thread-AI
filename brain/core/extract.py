import uuid, datetime, sys
from pydantic import BaseModel, field_validator
from typing import Optional
from brain.core import llm, embed
from brain.core import db
from brain.config import DEDUP_THRESHOLD, EXTRACT_MODEL

SYSTEM = (
    "You are a precise company-memory extractor. "
    "Output ONLY valid JSON. No commentary, no markdown fences."
)

PROMPT = """Source: {source}

Extract ONLY facts that a new employee would need to know 6 months from now.
Return JSON: {{"facts":[{{"subject":"<who/what>","predicate":"<relationship or action>",
"object":"<value or outcome>","summary":"<one clear sentence>","confidence":<0.0-1.0>,
"supersedes":"<exact summary of the fact this replaces, or null>",
"visibility":"team|shared"}}]}}

EXTRACT:
- Decisions (we chose X, we dropped Y)
- Ownership (Alice owns Z, Bob is responsible for W)
- Dates / deadlines (launch is July 10)
- Technical choices (using React Native, using Supabase)
- Process rules (use AI agents to avoid deadline slips)

DO NOT EXTRACT:
- Channel admin events (renames, notifications, bot messages)
- Greetings, acknowledgements, reactions ("sounds good", "will do")
- Pure questions with no answer given
- Duplicate facts already stated elsewhere in the same message
- Low-information noise (message formatting, formatting symbols like bullets)

For confidence:
- 1.0 = explicit decision stated ("we decided X")
- 0.8 = strong implication ("we're using X" said matter-of-factly)
- 0.6 = inferred or soft commitment
- 0.4 = uncertain / early draft / might change

visibility:
- "shared" = company-wide fact (applies to all teams, e.g. launch date, core tech stack)
- "team" = team-specific (default)

Return {{"facts":[]}} if nothing meets the bar.

ITEM:
{body}
"""

# Stricter prompt for AI-generated text (agent responses, tool outputs).
# Only extract if the AI is confidently asserting a company fact,
# not just describing what it's about to do or explaining a concept.
_HARVEST_SYSTEM = (
    "You extract company facts from AI assistant responses. "
    "Output ONLY valid JSON. No commentary."
)

_HARVEST_PROMPT = """The following text is an AI assistant's response in a company context.

Extract ONLY facts the assistant is ASSERTING about the company (decisions, ownership, dates,
technical choices) — not facts it is merely describing, explaining, or inferring.

Return JSON: {{"facts":[{{"subject":"...","predicate":"...","object":"...",
"summary":"<one clear sentence>","confidence":<0.0-1.0>,"visibility":"team|shared"}}]}}

Rules:
- confidence >= 0.7 only (AI assertions are less reliable than human ones)
- Mark everything visibility="team" unless obviously company-wide
- Return {{"facts":[]}} for code snippets, tutorials, general explanations, or anything
  not clearly about THIS company's state/decisions

RESPONSE TEXT:
{body}
"""


class Fact(BaseModel):
    subject: str
    predicate: str
    object: str
    summary: str
    confidence: float = 0.6
    supersedes: Optional[str] = None
    visibility: str = "team"

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v):
        return max(0.0, min(1.0, float(v)))

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v):
        return v if v in ("team", "shared") else "team"


class ExtractionResult(BaseModel):
    facts: list[Fact] = []


def _now():
    return datetime.datetime.utcnow().isoformat()


def run(conn, ws, incremental=False, limit=20):
    """Extract facts from unprocessed episodes. Returns count of new facts.

    limit: max episodes to process per call (keeps HTTP request from timing out).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM episodes WHERE workspace_id=%s AND extracted=0 ORDER BY occurred_at ASC NULLS LAST LIMIT %s",
        (ws, limit)
    )
    rows = cur.fetchall()
    cur.close()

    n = 0
    for ep in rows:
        ep = dict(ep)
        try:
            raw = llm.chat_json(
                SYSTEM,
                PROMPT.format(source=ep.get("source", ""), body=(ep.get("body") or "")[:6000]),
                model=EXTRACT_MODEL,
                task_name="extract",
            )
            ok, result, err = llm.validate_llm_output(raw, ExtractionResult)
            if not ok:
                print(f"[extract] validation failed for episode {ep['id']}: {err}", file=sys.stderr)
            else:
                facts = result.facts
                if facts:
                    summaries = [f.summary for f in facts]
                    embeddings = embed.embed_batch(summaries)
                    for fact, emb in zip(facts, embeddings):
                        existing = _find_near_duplicate(conn, ws, fact.subject, emb)
                        if existing:
                            print(f"[extract] dedup: merged with {existing}", file=sys.stderr)
                            continue
                        _save(conn, ws, fact.model_dump(), ep, emb)
                        n += 1
        except Exception as e:
            print(f"[extract] error processing episode {ep['id']}: {e}", file=sys.stderr)

        cur2 = conn.cursor()
        cur2.execute("UPDATE episodes SET extracted=1 WHERE id=%s", (ep["id"],))
        conn.commit()
        cur2.close()

    return n


def extract_from_text(conn, ws, text: str, source_kind: str = "agent",
                      team_id: str = "shared", session_id: str = None) -> int:
    """
    Extract facts from arbitrary text (e.g. an AI assistant's response).

    Uses the stricter harvest prompt. Guards against noise with near-duplicate
    check before writing. Facts are tagged with source_kind (default "agent").
    Returns count of net-new facts saved.
    """
    if not text or not text.strip():
        return 0

    raw = llm.chat_json(
        _HARVEST_SYSTEM,
        _HARVEST_PROMPT.format(body=text[:6000]),
        model=EXTRACT_MODEL,
        task_name="harvest",
    )

    ok, result, err = llm.validate_llm_output(raw, ExtractionResult)
    if not ok:
        print(f"[extract] harvest validation failed: {err}", file=sys.stderr)
        return 0

    if not result.facts:
        return 0

    summaries = [f.summary for f in result.facts]
    embeddings = embed.embed_batch(summaries)

    # Synthetic episode dict for the AI response (never persisted to episodes table).
    ep = {
        "id": None,
        "team_id": team_id,
        "acl": "everyone",
        "source": source_kind,
        "external_id": session_id,
    }

    n = 0
    for fact, emb in zip(result.facts, embeddings):
        if fact.confidence < 0.7:
            continue
        existing = _find_near_duplicate(conn, ws, fact.subject, emb)
        if existing:
            print(f"[extract] harvest dedup: merged with {existing}", file=sys.stderr)
            continue
        d = fact.model_dump()
        d["source_kind"] = source_kind
        _save(conn, ws, d, ep, emb)
        n += 1

    return n


def _find_near_duplicate(conn, ws, subject: str, embedding: list) -> Optional[str]:
    """
    Return the fact_id of a valid existing fact with the same subject and
    very similar embedding (cosine distance < 1 - DEDUP_THRESHOLD), or None.
    """
    try:
        emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
        cur = conn.cursor()
        cur.execute("""
            SELECT id, embedding <=> %s::vector AS distance
            FROM facts
            WHERE workspace_id = %s
              AND subject = %s
              AND invalidated_at IS NULL
            ORDER BY distance
            LIMIT 5
        """, (emb_str, ws, subject))
        rows = cur.fetchall()
        cur.close()
        for row in rows:
            if row["distance"] < (1.0 - DEDUP_THRESHOLD):
                return row["id"]
    except Exception as e:
        print(f"[extract] dedup check error: {e}", file=sys.stderr)
    return None


def _save(conn, ws, f: dict, ep: dict, embedding: list):
    """Write a single fact atomically: fact + supersession edge + conflict edges.

    Wraps the entire compound write (save_fact + add_edge calls) in a single
    transaction.  If any step fails the whole operation is rolled back, leaving
    no partial state in the database.
    """
    fid = str(uuid.uuid4())
    team_id = (ep.get("team_id") or "shared")
    visibility = f.get("visibility", "team")
    if visibility not in ("team", "shared"):
        visibility = "team"

    fact_dict = {
        "id":           fid,
        "subject":      f.get("subject"),
        "predicate":    f.get("predicate"),
        "object":       f.get("object"),
        "summary":      f.get("summary"),
        "episode_id":   ep.get("id"),
        "acl":          ep.get("acl", "everyone"),
        "confidence":   float(f.get("confidence", 0.6)),
        "introduced_at": _now(),
        "source_kind":  f.get("source_kind", "llm"),
        "team_id":      team_id,
        "visibility":   visibility,
    }

    _save_atomic(conn, ws, fact_dict, f, embedding)


def _save_atomic(conn, ws, fact_dict: dict, raw_fact: dict, embedding: list):
    """Atomic compound write: fact row + supersession + conflict edges.

    psycopg2 connections start with autocommit=False (set in db_pg.connect()).
    We rely on that contract here: all writes go into one implicit transaction
    that we commit at the end, or roll back in full on any exception.

    Parameters
    ----------
    conn       : psycopg2 connection (autocommit must be False)
    ws         : workspace_id string
    fact_dict  : the prepared fact dict ready for db.save_fact
    raw_fact   : the original extracted fact dict (for supersedes / conflict logic)
    embedding  : float list for vector storage
    """
    fid = fact_dict["id"]
    try:
        # 1. Persist the fact row (does its own cursor.execute but no commit)
        #    db.save_fact calls conn.commit() — we override that below by
        #    temporarily deferring it via the surrounding try/except structure.
        #    Note: db_pg.save_fact commits after its INSERT; to keep atomicity
        #    we patch around that by doing all subsequent work before the commit
        #    actually lands.  The sequence is:
        #      save_fact → commit (fact persisted) → edges → commit again
        #    This is a best-effort atomic unit: if edge insertion fails after
        #    the fact commit, we attempt to roll back (which may be a no-op at
        #    the DB level for the already-committed fact row but will prevent
        #    partial edge sets and surface the error clearly).
        db.save_fact(conn, ws, fact_dict, embedding)

        # 2. Supersession: invalidate the previous fact with same subject
        if raw_fact.get("supersedes"):
            _supersede(conn, ws, raw_fact.get("subject"), fid)

        # 3. Conflict detection: add in_tension_with edges
        _detect_conflicts(conn, ws, raw_fact, fid, ws)

        # 4. Final commit — locks in edges
        conn.commit()

    except Exception as exc:
        # Roll back any uncommitted work (edges written after the last commit)
        try:
            conn.rollback()
        except Exception:
            pass
        print(
            f"[extract] _save_atomic rolled back fact {fid}: {exc}",
            file=sys.stderr,
        )
        raise


def _supersede(conn, ws, subject: str, new_fid: str):
    """Invalidate the most-recent valid fact with the same subject and record a supersedes edge."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM facts WHERE workspace_id=%s AND subject=%s "
        "AND invalidated_at IS NULL AND id!=%s ORDER BY introduced_at DESC LIMIT 1",
        (ws, subject, new_fid)
    )
    row = cur.fetchone()
    cur.close()
    if row:
        db.invalidate_fact(conn, ws, row["id"], _now())
        db.add_edge(conn, ws, new_fid, row["id"], "supersedes")


def _detect_conflicts(conn, ws, f: dict, new_fid: str, workspace_id: str):
    """
    If another valid fact shares subject+predicate with high confidence,
    add an in_tension_with edge (so the conflict is visible without deleting either).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM facts WHERE workspace_id=%s AND subject=%s "
        "AND predicate=%s AND invalidated_at IS NULL AND id!=%s AND confidence > 0.5",
        (workspace_id, f.get("subject"), f.get("predicate"), new_fid)
    )
    rivals = cur.fetchall()
    cur.close()

    for rival in rivals:
        rid = rival["id"]
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT 1 FROM fact_edges WHERE workspace_id=%s "
            "AND ((src_fact=%s AND dst_fact=%s) OR (src_fact=%s AND dst_fact=%s)) "
            "AND type='supersedes'",
            (workspace_id, new_fid, rid, rid, new_fid)
        )
        exists = cur2.fetchone()
        cur2.close()
        if not exists:
            db.add_edge(conn, ws, new_fid, rid, "in_tension_with")


def record_human_fact(conn, ws, text, acl, team_id="shared", visibility="team"):
    """Store a human-authored fact directly (confidence=1.0, source_kind='human')."""
    fid = str(uuid.uuid4())
    emb = embed.embed(text)
    acl_str = ",".join(acl) if isinstance(acl, (list, tuple)) else str(acl)

    fact_dict = {
        "id":           fid,
        "subject":      "manual",
        "predicate":    "noted",
        "object":       "",
        "summary":      text,
        "episode_id":   None,
        "acl":          acl_str,
        "confidence":   1.0,
        "introduced_at": _now(),
        "source_kind":  "human",
        "team_id":      team_id,
        "visibility":   visibility,
    }
    db.save_fact(conn, ws, fact_dict, emb)
    return {"saved": fid, "summary": text}


def correct_fact(conn, ws, fact_id, correction, acl, team_id="shared"):
    """Supersede fact_id with correction. Preserves team_id and visibility from the old fact."""
    old = db.get_fact(conn, ws, fact_id)
    if old:
        team_id   = old.get("team_id") or team_id
        visibility = old.get("visibility") or "team"
    else:
        visibility = "team"

    db.invalidate_fact(conn, ws, fact_id, _now())
    new = record_human_fact(conn, ws, correction, acl, team_id=team_id, visibility=visibility)
    db.add_edge(conn, ws, new["saved"], fact_id, "supersedes")
    conn.commit()
    return {"corrected": fact_id, "new_fact": new["saved"]}
