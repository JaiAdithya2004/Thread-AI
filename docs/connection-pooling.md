# Connection Pooling

## Why PgBouncer?

Thread's FastAPI server uses **psycopg2** (synchronous) with `uvicorn`'s default
thread-per-request model. Each concurrent HTTP request holds an open Postgres
connection for the lifetime of the request. Without a pooler:

- 100 concurrent requests = 100 open Postgres connections
- Postgres's default `max_connections = 100` is exhausted immediately under load
- Connection setup cost (~5 ms each) adds latency to every request

PgBouncer in **transaction mode** solves this by multiplexing many app connections
onto a small server-side pool. A psycopg2 connection is handed back to PgBouncer
the moment each SQL transaction completes, not at request end.

## Running PgBouncer Locally

1. **Install** (Linux/macOS):
   ```bash
   # macOS
   brew install pgbouncer

   # Ubuntu/Debian
   sudo apt install pgbouncer
   ```

2. **Configure** — copy `pgbouncer/pgbouncer.ini` to `/etc/pgbouncer/pgbouncer.ini`
   and create `/etc/pgbouncer/userlist.txt` from `pgbouncer/userlist.txt.example`:
   ```
   "brain" "brain_local"
   ```
   The format is: `"username" "password"` (plain text for local dev; use MD5 hashes
   in production — run `echo "md5$(echo -n 'passwordusername' | md5sum | cut -d' ' -f1)"`).

3. **Start**:
   ```bash
   pgbouncer /etc/pgbouncer/pgbouncer.ini
   ```

4. **Point the app at the pooler** — change `DATABASE_URL` in `.env`:
   ```
   DATABASE_URL=postgresql://brain:brain_local@127.0.0.1:5433/brain
   ```
   Note port **5433** (PgBouncer) instead of 5432 (Postgres direct).

5. **Verify**:
   ```bash
   psql -h 127.0.0.1 -p 5433 -U brain brain -c "SHOW pools;"
   ```

## Configuration Rationale

| Parameter | Value | Why |
|---|---|---|
| `pool_mode` | `transaction` | Connection released after each transaction — correct for psycopg2's connection-per-request pattern |
| `max_client_conn` | 200 | Allows 200 app connections total (across all uvicorn workers) |
| `default_pool_size` | 20 | 20 real Postgres connections, sufficient for ~100 concurrent users at typical query latency |
| `server_reset_query` | `RESET ALL` | Clears session-local settings (e.g. `brain.workspace_id`) between clients |

**Important:** `RESET ALL` is required because the app uses `SET LOCAL`/`set_config()`
to set RLS session variables (`brain.workspace_id`, `brain.user_id`). Without it,
a session-level `set_config` from one request could leak into the next request
served by the same Postgres backend.

## Cloud / Production: Supabase Pooler

If you deploy on Supabase, use their built-in **Transaction Pooler** instead of
running PgBouncer yourself. Just swap `DATABASE_URL` to the pooler URL found in
your Supabase project settings under **Database → Connection Pooling**:

```
DATABASE_URL=postgresql://postgres.[project-ref]:[password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

The pooler port is **6543** (transaction mode) vs 5432 (direct/session mode).
No other code changes required.

## Caveats with Transaction Pooling

- **Prepared statements** are not supported in transaction mode — psycopg2 uses
  simple query protocol by default so this is not an issue.
- **`SET` / `set_config` without `is_local=true`** can persist across requests.
  The app uses `set_config(..., false)` (session-level) for RLS vars; the
  `server_reset_query = RESET ALL` in pgbouncer.ini clears these between
  connections being recycled.
- **Advisory locks** held across transactions will not work — not used by Thread.
