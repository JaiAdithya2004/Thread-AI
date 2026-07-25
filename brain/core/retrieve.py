import json, hashlib, sys
from brain.core import db, embed, llm
from brain.config import (
    RERANK_ENABLED, RERANK_MODEL, REDIS_URL, CACHE_TTL,
    CONFIDENCE_THRESHOLD, TOP_FACTS, SYNTH_MODEL,
)

# Cross-encoder reranker — loaded lazily on first use
_reranker = None
_reranker_loaded = False

# Redis cache singleton (lazy)
_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis as _redis_lib
        _redis = _redis_lib.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1)
        _redis.ping()
        return _redis
    except Exception:
        _redis = None
        return None


def _cache_key(embedding: list) -> str:
    return "brain:qcache:" + hashlib.md5(str(embedding).encode()).hexdigest()


def _vec(conn, query_emb, n=30):
    return db.search_vec(conn, query_emb, n)


def _fts(conn, query, n=30):
    return db.search_fts(conn, query, n)


def _rrf(*lists, k=60):
    s = {}
    for lst in lists:
        for rank, fid in enumerate(lst):
            s[fid] = s.get(fid, 0) + 1.0 / (k + rank)
    return sorted(s, key=s.get, reverse=True)


def _expand(query):
    try:
        r = llm.chat_json(
            'Return JSON {"terms":[...]} of related search terms.',
            f"Query: {query}",
            task_name="expand")
        return query + " " + " ".join(r.get("terms", [])[:5])
    except Exception:
        return query


def _allowed(acl_str, principals):
    acl = (acl_str or "everyone").split(",")
    return "everyone" in acl or any(p in acl for p in principals)


def _team_visible(row, team_id, cross_team):
    if cross_team:
        return True
    vis = row["visibility"] or "team"
    if vis == "shared":
        return True
    return (row["team_id"] or "shared") == (team_id or "shared")


def _get_reranker():
    global _reranker, _reranker_loaded
    if _reranker_loaded:
        return _reranker
    _reranker_loaded = True
    if not RERANK_ENABLED:
        return None
    try:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANK_MODEL)
    except Exception as e:
        print(f"[retrieve] reranker unavailable ({e}), disabled", file=sys.stderr)
        _reranker = None
    return _reranker


def _rerank(query: str, facts: list) -> list:
    reranker = _get_reranker()
    if reranker is None or not facts:
        return facts
    try:
        pairs = [(query, f["summary"]) for f in facts]
        scores = reranker.predict(pairs)
        ranked = sorted(zip(scores, facts), key=lambda x: x[0], reverse=True)
        return [f for _, f in ranked]
    except Exception as e:
        print(f"[retrieve] rerank failed: {e}", file=sys.stderr)
        return facts


def synthesize_answer(query: str, facts: list) -> str:
    if not facts:
        return ""
    fact_lines = "\n".join(
        f"[{f.get('fact_id','?')}] {f['summary']}"
        for f in facts
    )
    system = (
        "You are a company knowledge assistant. "
        "Answer the question using ONLY the facts provided — do not add outside knowledge. "
        "Be direct and concise (1-3 sentences). "
        "Cite each fact you use with its ID in square brackets, e.g. [abc123]. "
        "If the facts don't fully answer the question, say what you know and note the gap. "
        "If NO fact is relevant, reply: \"I don't have that information in company memory.\""
    )
    user = (
        f"Facts from company memory:\n{fact_lines}\n\n"
        f"Question: {query}\n\n"
        "Answer (cite fact IDs):"
    )
    return llm.chat_text(system, user, model=SYNTH_MODEL, task_name="synthesize")


def run(conn, ws, query, principals, caller, surface,
        team_id=None, include_invalid=False, cross_team=False, top=None):
    top = top or TOP_FACTS
    q_emb = embed.embed(query)

    # Redis cache check
    cache_key = _cache_key(q_emb)
    r = _get_redis()
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    vec_hits = _vec(conn, q_emb)
    # Skip query expansion (an LLM call) if there are no vec hits to expand against
    expanded = _expand(query) if vec_hits else query
    fused = _rrf(vec_hits, _fts(conn, expanded))

    facts = []
    _ph = "%s" if db.BACKEND == "postgres" else "?"
    for fid in fused:
        if db.BACKEND == "postgres":
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM facts WHERE id={_ph} AND workspace_id={_ph}", (fid, ws))
            row = cur.fetchone()
            cur.close()
        else:
            row = conn.execute(
                "SELECT * FROM facts WHERE id=? AND workspace_id=?", (fid, ws)).fetchone()
        if not row:
            continue
        row = dict(row)
        if not _allowed(row["acl"], principals):
            continue
        if not _team_visible(row, team_id, cross_team):
            continue
        if row["invalidated_at"] and not include_invalid:
            continue
        facts.append(row)

    facts.sort(key=lambda f: (f["invalidated_at"] is not None, fused.index(f["id"])))

    # Rerank top 30, then take top N
    facts = _rerank(query, facts[:30])[:top]

    db.log(conn, ws, caller, query, surface, [f["id"] for f in facts],
           "\n".join(f["summary"] for f in facts), team_id=team_id)

    # Store in cache
    if r:
        try:
            r.setex(cache_key, CACHE_TTL, json.dumps(facts))
        except Exception:
            pass

    return facts


def _cite(f):
    tag = "stale" if f["invalidated_at"] else "valid"
    return {
        "summary": f["summary"],
        "status": tag,
        "confidence": round(f["confidence"] or 0, 2),
        "source": f["source_kind"],
        "fact_id": f["id"],
        "team": f.get("team_id"),
        "visibility": f.get("visibility"),
        "episode_id": f.get("episode_id"),
    }


def format_answer(query, facts):
    cited = [_cite(f) for f in facts]
    top_conf = (facts[0]["confidence"] or 0) if facts else 0

    # "Not sure" path: no facts, or best match is low-confidence, or ALL facts are stale
    all_stale = facts and all(f["invalidated_at"] for f in facts)
    if not facts or top_conf < CONFIDENCE_THRESHOLD or all_stale:
        return {
            "answer": "I don't have reliable information about that in company memory.",
            "facts": cited[:3],
            "synthesized_answer": "",
        }
    synthesized = synthesize_answer(query, cited[:8])
    return {
        "answer": "From company memory:",
        "facts": cited,
        "synthesized_answer": synthesized,
    }


def format_history(subject, facts):
    facts.sort(key=lambda f: f["introduced_at"] or "")
    return {"subject": subject, "timeline": [_cite(f) for f in facts]}
