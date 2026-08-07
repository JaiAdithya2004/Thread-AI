import uuid, datetime, sys
from pydantic import BaseModel, field_validator
from typing import Optional
from brain.core import llm, embed
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

# Lighter prompt for AI-generated text (agent responses, AI tool outputs).
# Stricter: only extract if the AI is confidently asserting a company fact,
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


def run(conn, ws, incremental=False):
    rows = conn.execute(
        "SELECT * FROM episodes WHERE workspace_id=? AND extracted=0"
        " ORDER BY occurred_at ASC NULLS LAST",
        (ws,)).fetchall()
    n = 0
    for ep in rows:
        try:
            raw = llm.chat_json(SYSTEM, PROMPT.format(
                source=ep["source"], body=ep["body"][:6000]),
                model=EXTRACT_MODEL, task_name="extract")

            ok, result, err = llm.validate_llm_output(raw, ExtractionResult)
            if not ok:
                print(f"[extract] validation failed for episode {ep['id']}: {err}", file=sys.stderr)
                conn.execute("UPDATE episodes SET extracted=1 WHERE id=?", (ep["id"],))
                conn.commit()
                continue

            facts = result.facts
            if not facts:
                conn.execute("UPDATE episodes SET extracted=1 WHERE id=?", (ep["id"],))
                conn.commit()
                continue

            # Batch-embed all summaries at once
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

        conn.execute("UPDATE episodes SET extracted=1 WHERE id=?", (ep["id"],))
        conn.commit()
    return n


def extract_from_text(conn, ws, text: str, source_kind: str = "ai-tool",
                      team_id: str = "shared", session_id: str = None) -> int:
    """
    Extract facts from arbitrary text (e.g. an AI assistant's response).
    Uses the stricter harvest prompt. Guards against noise with near-duplicate
    check before writing. Returns number of new facts saved.

    source_kind should be one of: "ai-tool", "human", "llm".
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

    # Synthetic episode for the AI response — not persisted to episodes table,
    # we use a fake ep dict that _save() can read field names from.
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
    """Find a valid fact with same subject and very similar embedding. Returns fact_id or None."""
    import struct
    try:
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        rows = conn.execute(
            "SELECT fact_id, distance FROM fact_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
            (blob,)
        ).fetchall()
        for row in rows:
            fid = row[0]
            dist = row[1]
            if dist < (1 - DEDUP_THRESHOLD):
                fact = conn.execute(
                    "SELECT subject, invalidated_at FROM facts WHERE id=? AND workspace_id=?",
                    (fid, ws)
                ).fetchone()
                if fact and fact["subject"] == subject and not fact["invalidated_at"]:
                    return fid
    except Exception:
        pass
    return None


def _save(conn, ws, f: dict, ep, embedding: list):
    fid = str(uuid.uuid4())
    team_id = ep["team_id"] if ep["team_id"] else "shared"
    visibility = f.get("visibility", "team")
    if visibility not in ("team", "shared"):
        visibility = "team"

    conn.execute(
        """INSERT INTO facts(id,workspace_id,subject,predicate,object,summary,
           episode_id,acl,confidence,introduced_at,source_kind,team_id,visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, ws, f.get("subject"), f.get("predicate"), f.get("object"),
         f.get("summary"), ep.get("id"), ep.get("acl", "everyone"),
         float(f.get("confidence", 0.6)), _now(),
         f.get("source_kind", "llm"), team_id, visibility))

    # FTS index
    conn.execute("INSERT INTO fact_fts(fact_id,summary,subject,object) VALUES (?,?,?,?)",
                 (fid, f.get("summary", ""), f.get("subject", ""), f.get("object", "")))

    # Vec index
    import struct
    blob = struct.pack(f"{len(embedding)}f", *embedding)
    conn.execute("INSERT OR REPLACE INTO fact_vec(fact_id,embedding) VALUES (?,?)", (fid, blob))

    if f.get("supersedes"):
        _supersede(conn, ws, f.get("subject"), fid)

    _detect_conflicts(conn, ws, f, fid)
    conn.commit()


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


def _detect_conflicts(conn, ws, f: dict, new_fid: str):
    """If another valid fact has same subject+predicate with high confidence, add in_tension_with edge."""
    rivals = conn.execute(
        "SELECT id, confidence FROM facts WHERE workspace_id=? AND subject=? "
        "AND predicate=? AND invalidated_at IS NULL AND id!=? AND confidence > 0.5",
        (ws, f.get("subject"), f.get("predicate"), new_fid)
    ).fetchall()
    for rival in rivals:
        edge_exists = conn.execute(
            "SELECT 1 FROM fact_edges WHERE workspace_id=? "
            "AND ((src_fact=? AND dst_fact=?) OR (src_fact=? AND dst_fact=?)) "
            "AND type='supersedes'",
            (ws, new_fid, rival["id"], rival["id"], new_fid)
        ).fetchone()
        if not edge_exists:
            conn.execute(
                "INSERT INTO fact_edges(workspace_id,src_fact,dst_fact,type) VALUES (?,?,?,?)",
                (ws, new_fid, rival["id"], "in_tension_with"))


def record_human_fact(conn, ws, text, acl, team_id="shared", visibility="team"):
    fid = str(uuid.uuid4())
    emb = embed.embed(text)
    conn.execute(
        """INSERT INTO facts(id,workspace_id,subject,predicate,object,summary,
           episode_id,acl,confidence,introduced_at,source_kind,team_id,visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, ws, "manual", "noted", "", text, None, ",".join(acl),
         1.0, _now(), "human", team_id, visibility))
    conn.execute("INSERT INTO fact_fts(fact_id,summary,subject,object) VALUES (?,?,?,?)",
                 (fid, text, "manual", ""))
    import struct
    blob = struct.pack(f"{len(emb)}f", *emb)
    conn.execute("INSERT OR REPLACE INTO fact_vec(fact_id,embedding) VALUES (?,?)", (fid, blob))
    conn.commit()
    return {"saved": fid, "summary": text}


def correct_fact(conn, ws, fact_id, correction, acl, team_id="shared"):
    old = conn.execute(
        "SELECT team_id, visibility FROM facts WHERE id=?", (fact_id,)).fetchone()
    if old:
        team_id = old["team_id"] or team_id
        visibility = old["visibility"] or "team"
    else:
        visibility = "team"
    conn.execute("UPDATE facts SET invalidated_at=? WHERE id=? AND workspace_id=?",
                 (_now(), fact_id, ws))
    new = record_human_fact(conn, ws, correction, acl, team_id=team_id, visibility=visibility)
    conn.execute(
        "INSERT INTO fact_edges(workspace_id,src_fact,dst_fact,type) VALUES (?,?,?,?)",
        (ws, new["saved"], fact_id, "supersedes"))
    conn.commit()
    return {"corrected": fact_id, "new_fact": new["saved"]}
