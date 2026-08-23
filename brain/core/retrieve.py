import json, hashlib, sys
import hashlib as _hashlib
from brain.core import db, llm
from brain.core.resilience import get_breaker, CircuitOpen
from brain.config import (
    RERANK_ENABLED, RERANK_MODEL, REDIS_URL, CACHE_TTL,
    CONFIDENCE_THRESHOLD, TOP_FACTS, SYNTH_MODEL,
)

# ── In-process embedding cache (avoids Gemini round-trip for repeated queries) ──
# CPython dict reads/writes are GIL-protected so this is safe for concurrent requests.
_embed_cache: dict = {}   # query_hash -> vector
_EMBED_CACHE_MAX = 256


def _cached_embed(text: str) -> list:
    """Return the embedding for *text*, hitting the in-process LRU dict first."""
    key = _hashlib.md5(text.lower().strip().encode()).hexdigest()
    if key in _embed_cache:
        return _embed_cache[key]
    from brain.core.embed import embed_texts as _embed_texts
    vecs = _embed_texts([text], task_type="RETRIEVAL_QUERY")
    vec = vecs[0] if vecs else [0.0] * 768
    if len(_embed_cache) >= _EMBED_CACHE_MAX:
        # Evict the oldest (insertion-order) entry
        _embed_cache.pop(next(iter(_embed_cache)))
    _embed_cache[key] = vec
    return vec


# Cross-encoder reranker — loaded lazily on first use
_reranker = None
_reranker_loaded = False

# Redis cache singleton (lazy init, replaced with None on failure)
_redis = None
_redis_checked = False

# Circuit breaker for Redis — trips after 3 consecutive failures, recovers after 60 s
_redis_breaker = get_breaker("redis", failure_threshold=3, recovery_timeout=60.0)


def _get_redis():
    """Return a live Redis client or None if Redis is unavailable."""
    global _redis, _redis_checked
    if _redis_checked and _redis is not None:
        return _redis
    try:
        with _redis_breaker:
            import redis as _redis_lib
            client = _redis_lib.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
            client.ping()
            _redis = client
            _redis_checked = True
            return _redis
    except CircuitOpen:
        return None
    except Exception:
        _redis_checked = False  # allow re-probe next call
        _redis = None
        return None


def _cache_get(key: str):
    """Return cached value or None; swallows all Redis errors."""
    r = _get_redis()
    if r is None:
        return None
    try:
        with _redis_breaker:
            val = r.get(key)
            return json.loads(val) if val else None
    except CircuitOpen:
        return None
    except Exception as e:
        print(f"[retrieve] Redis get failed (falling through): {e}", file=sys.stderr)
        return None


def _cache_set(key: str, value, ttl: int):
    """Set cache value; swallows all Redis errors."""
    r = _get_redis()
    if r is None:
        return
    try:
        with _redis_breaker:
            r.setex(key, ttl, json.dumps(value))
    except CircuitOpen:
        pass
    except Exception as e:
        print(f"[retrieve] Redis set failed (non-fatal): {e}", file=sys.stderr)


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
    """Query expansion via LLM — returns original query on any failure."""
    try:
        r = llm.chat_json(
            'Return JSON {"terms":[...]} of related search terms.',
            f"Query: {query}",
            task_name="expand",
        )
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
    """
    LLM-synthesized answer.  Returns "" gracefully if the LLM circuit is open
    or the LLM returns empty — callers always check for empty and fall back to
    returning just the cited facts.
    """
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

    # Embed the query — uses in-process cache to skip Gemini round-trip on repeated queries
    q_emb = _cached_embed(query)

    # Redis cache check — falls through silently if Redis is down
    cache_key = _cache_key(q_emb)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    vec_hits = _vec(conn, q_emb)
    # Only expand when we have enough vec hits — expansion adds ~1-2 s of LLM latency
    if len(vec_hits) >= 3:
        expanded = _expand(query)
        fts_hits = _fts(conn, expanded)
    else:
        fts_hits = _fts(conn, query)
    fused = _rrf(vec_hits, fts_hits)

    facts = []
    for fid in fused:
        cur = conn.cursor()
        cur.execute("SELECT * FROM facts WHERE id=%s AND workspace_id=%s", (fid, ws))
        row = cur.fetchone()
        cur.close()
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
    facts = _rerank(query, facts[:30])[:top]

    db.log(conn, ws, caller, query, surface, [f["id"] for f in facts],
           "\n".join(f["summary"] for f in facts), team_id=team_id)

    # Write to cache — falls through silently if Redis is down
    _cache_set(cache_key, facts, CACHE_TTL)

    return facts


def _cite(f):
    tag = "stale" if f["invalidated_at"] else "valid"
    return {
        "summary":    f["summary"],
        "status":     tag,
        "confidence": round(f["confidence"] or 0, 2),
        "source":     f["source_kind"],
        "fact_id":    f["id"],
        "team":       f.get("team_id"),
        "visibility": f.get("visibility"),
        "episode_id": f.get("episode_id"),
    }


def format_answer(query, facts):
    cited    = [_cite(f) for f in facts]
    top_conf = (facts[0]["confidence"] or 0) if facts else 0
    all_stale = facts and all(f["invalidated_at"] for f in facts)

    if not facts or top_conf < CONFIDENCE_THRESHOLD or all_stale:
        return {
            "answer":            "I don't have reliable information about that in company memory.",
            "facts":             cited[:3],
            "synthesized_answer": "",
        }

    # Attempt LLM synthesis — gracefully degrade to cited-only if LLM is down
    # Cap at 5 facts to keep the prompt short and reduce synthesis latency
    synthesized = synthesize_answer(query, cited[:5])
    if not synthesized:
        # LLM unavailable or circuit open — return facts without synthesis
        return {
            "answer":            "From company memory (synthesis unavailable):",
            "facts":             cited,
            "synthesized_answer": "",
        }

    return {
        "answer":            "From company memory:",
        "facts":             cited,
        "synthesized_answer": synthesized,
    }


def format_history(subject, facts):
    facts.sort(key=lambda f: f["introduced_at"] or "")
    return {"subject": subject, "timeline": [_cite(f) for f in facts]}
