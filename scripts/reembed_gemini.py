#!/usr/bin/env python3
"""
Re-embed all facts using gemini-embedding-001 (768-dim).

Usage:
    python scripts/reembed_gemini.py [--dry-run] [--reset-progress]

Options:
    --dry-run          Show what would be done, don't write to DB
    --reset-progress   Clear the resume checkpoint and start from the beginning

This is Phase 3 of the gemini-embedding-001 migration.
Safe to run multiple times -- skips facts that already have fact_vec_768 populated.
Run this AFTER Phase 2 migration (fact_vec_768 column must exist).
Run BEFORE Phase 4 cutover.
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
EMBED_BATCH_SIZE = 64
BATCH_SLEEP = 0.5          # seconds between batches (safe for free tier)
RETRY_SLEEP_429 = 60.0     # back-off on HTTP 429
RETRY_SLEEP_5XX = 10.0     # back-off on HTTP 5xx

_SCRIPT_DIR = Path(__file__).parent
PROGRESS_FILE = _SCRIPT_DIR / ".reembed_progress.json"

GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:batchEmbedContents"
)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _l2_normalize(vec: list[float]) -> list[float]:
    """Return the L2-normalized version of vec."""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def embed_batch(
    texts: list[str],
    api_key: str,
    model: str = EMBED_MODEL,
    dim: int = EMBED_DIM,
) -> list[list[float]]:
    """
    Call the Gemini batchEmbedContents endpoint and return normalized vectors.

    Parameters
    ----------
    texts:   non-empty list of strings to embed (max 100 per Gemini batch)
    api_key: Gemini API key (never logged)
    model:   embedding model id
    dim:     outputDimensionality for the model

    Returns
    -------
    list of L2-normalized float vectors, same length as texts

    Raises
    ------
    requests.HTTPError on any HTTP error (callers handle retry logic)
    """
    url = GEMINI_EMBED_URL.format(model=model)
    payload = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": dim,
            }
            for t in texts
        ]
    }
    resp = requests.post(
        url,
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    embeddings = data.get("embeddings", [])
    if len(embeddings) != len(texts):
        raise ValueError(
            f"Gemini returned {len(embeddings)} embeddings for {len(texts)} texts"
        )

    return [_l2_normalize(e["values"]) for e in embeddings]


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress() -> dict:
    """Return saved progress dict, or fresh defaults."""
    if PROGRESS_FILE.exists():
        try:
            with PROGRESS_FILE.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            print(f"[warn] Could not read progress file ({exc}); starting from scratch.")
    return {"offset": 0, "done": 0, "failed": 0}


def save_progress(p: dict) -> None:
    """Persist progress dict to the hidden JSON file."""
    try:
        with PROGRESS_FILE.open("w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=2)
    except Exception as exc:
        print(f"[warn] Could not save progress file: {exc}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect(database_url: str):
    conn = psycopg2.connect(
        database_url,
        cursor_factory=psycopg2.extras.DictCursor,
    )
    conn.autocommit = False
    return conn


def _check_column_exists(conn) -> bool:
    """Return True if facts.fact_vec_768 column is present."""
    cur = conn.cursor()
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'facts'
          AND column_name = 'fact_vec_768'
        LIMIT 1
    """)
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def _count_pending(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM facts WHERE fact_vec_768 IS NULL")
    row = cur.fetchone()
    cur.close()
    return int(row["n"]) if row else 0


def _fetch_batch(conn, limit: int, offset: int) -> list:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, summary
        FROM facts
        WHERE fact_vec_768 IS NULL
        ORDER BY id
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    )
    rows = cur.fetchall()
    cur.close()
    return [{"id": r["id"], "summary": r["summary"]} for r in rows]


def _write_vectors(conn, id_vec_pairs: list[tuple]) -> None:
    """
    Bulk-update fact_vec_768.  Rolls back on error and re-raises.

    id_vec_pairs: list of (fact_id, vector_list)
    """
    params = []
    for fact_id, vector in id_vec_pairs:
        vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
        params.append((vec_str, fact_id))

    cur = conn.cursor()
    try:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE facts SET fact_vec_768 = %s::vector WHERE id = %s",
            params,
            page_size=EMBED_BATCH_SIZE,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False, reset_progress: bool = False) -> None:
    # -- API key check -------------------------------------------------------
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print("[error] GEMINI_API_KEY is not set. Export it and re-run.", file=sys.stderr)
        sys.exit(1)

    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://brain:brain_local@localhost:5432/brain",
    )

    # -- Connect + pre-flight checks -----------------------------------------
    print("[reembed] Connecting to Postgres...")
    try:
        conn = _connect(database_url)
    except Exception as exc:
        print(f"[error] Cannot connect to Postgres: {exc}", file=sys.stderr)
        sys.exit(1)

    if not _check_column_exists(conn):
        print(
            "[error] Column facts.fact_vec_768 does not exist. "
            "Run Phase 2 migration first (ALTER TABLE facts ADD COLUMN fact_vec_768 vector(768)).",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    total_pending = _count_pending(conn)
    print(f"[reembed] Facts needing embed: {total_pending}")
    if total_pending == 0:
        print("[reembed] Nothing to do.")
        conn.close()
        return

    # -- Progress checkpoint -------------------------------------------------
    if reset_progress and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("[reembed] Progress checkpoint cleared.")

    progress = load_progress()
    offset = progress["offset"]
    done = progress["done"]
    failed = progress["failed"]
    skipped = 0

    if offset > 0:
        print(f"[reembed] Resuming from offset {offset} ({done} already embedded).")

    if dry_run:
        print("[reembed] DRY RUN — no writes will be made.")

    # -- Main loop -----------------------------------------------------------
    while True:
        batch_rows = _fetch_batch(conn, EMBED_BATCH_SIZE, offset)
        if not batch_rows:
            break  # no more rows

        # Filter out empty summaries
        valid_rows = []
        for row in batch_rows:
            summary = (row["summary"] or "").strip()
            if not summary:
                print(f"[reembed] skipped {row['id']}: empty summary")
                skipped += 1
            else:
                valid_rows.append((row["id"], summary))

        if not valid_rows:
            offset += len(batch_rows)
            progress = {"offset": offset, "done": done, "failed": failed}
            save_progress(progress)
            continue

        ids = [r[0] for r in valid_rows]
        texts = [r[1] for r in valid_rows]

        if dry_run:
            print(
                f"[{done + len(valid_rows)}/{total_pending}] "
                f"DRY RUN: would embed batch of {len(valid_rows)} facts"
            )
            offset += len(batch_rows)
            done += len(valid_rows)
            progress = {"offset": offset, "done": done, "failed": failed}
            save_progress(progress)
            time.sleep(BATCH_SLEEP)
            continue

        # -- Embed with retry logic ------------------------------------------
        vectors = None
        try:
            vectors = embed_batch(texts, api_key, model=EMBED_MODEL, dim=EMBED_DIM)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                print(
                    f"[reembed] HTTP 429 (rate limited). Sleeping {RETRY_SLEEP_429}s then retrying..."
                )
                time.sleep(RETRY_SLEEP_429)
                try:
                    vectors = embed_batch(texts, api_key, model=EMBED_MODEL, dim=EMBED_DIM)
                except Exception as exc2:
                    print(f"[reembed] Retry after 429 also failed: {exc2}. Skipping batch.")
                    failed += 1
            elif 500 <= status < 600:
                print(
                    f"[reembed] HTTP {status} (server error). Sleeping {RETRY_SLEEP_5XX}s then retrying..."
                )
                time.sleep(RETRY_SLEEP_5XX)
                try:
                    vectors = embed_batch(texts, api_key, model=EMBED_MODEL, dim=EMBED_DIM)
                except Exception as exc2:
                    print(f"[reembed] Retry after {status} also failed: {exc2}. Skipping batch.")
                    failed += 1
            else:
                print(f"[reembed] HTTP {status} error on batch at offset {offset}. Skipping batch.")
                failed += 1
        except Exception as exc:
            print(f"[reembed] Embed error on batch at offset {offset}: {type(exc).__name__}. Skipping batch.")
            failed += 1

        if vectors is None:
            # Batch failed — advance offset so we don't retry same batch forever
            offset += len(batch_rows)
            progress = {"offset": offset, "done": done, "failed": failed}
            save_progress(progress)
            continue

        # -- Write vectors to DB --------------------------------------------
        id_vec_pairs = list(zip(ids, vectors))
        try:
            _write_vectors(conn, id_vec_pairs)
            done += len(id_vec_pairs)
            print(f"[{done}/{total_pending}] embedded batch of {len(id_vec_pairs)} facts")
        except Exception as exc:
            print(f"[reembed] DB write error for batch at offset {offset}: {type(exc).__name__}: {exc}. Skipping batch.")
            failed += 1

        offset += len(batch_rows)
        progress = {"offset": offset, "done": done, "failed": failed}
        save_progress(progress)

        time.sleep(BATCH_SLEEP)

    # -- Final summary -------------------------------------------------------
    conn.close()

    if not dry_run and PROGRESS_FILE.exists() and failed == 0:
        # Clean up checkpoint on successful completion
        PROGRESS_FILE.unlink()
        print("[reembed] Progress checkpoint removed (run complete).")

    print(
        f"\nDone. Total: {done} embedded, {failed} batches failed, {skipped} skipped."
    )
    if failed > 0:
        print(
            f"[reembed] {failed} batch(es) failed. Re-run to retry "
            "(progress is saved, completed facts will be skipped)."
        )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-embed all facts using gemini-embedding-001 (768-dim). "
                    "Phase 3 of the fastembed -> Gemini embedding migration."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done, don't write to DB or call the Gemini API.",
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help="Clear the resume checkpoint and start from the beginning.",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, reset_progress=args.reset_progress)
