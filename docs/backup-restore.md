# Backup and Restore

## Supabase automated backups

Supabase provides point-in-time recovery (PITR) on Pro and above plans:

- **Daily snapshots** — retained for 7 days on Pro, 30 days on Team/Enterprise.
- **Point-in-time recovery** — restore to any second within the retention window.
- Backups run automatically with no configuration required.
- To restore, open the Supabase dashboard → **Database** → **Backups** and select a point in time.

On the free plan, Supabase takes daily snapshots with a 1-day retention window. Upgrade to Pro for PITR.

---

## Manual backup with pg_dump

For an off-platform backup or before a risky migration:

```bash
pg_dump "$DATABASE_URL" \
  --format=custom \
  --no-owner \
  --no-acl \
  --file="brain-backup-$(date +%Y%m%d-%H%M%S).dump"
```

Store the `.dump` file in a secure, off-site location (S3, encrypted volume, etc.). The `--format=custom` flag produces a compressed binary dump that supports selective table restore.

To list the contents of a dump:

```bash
pg_restore --list brain-backup-*.dump
```

---

## Restore from a pg_dump file

Full restore to a fresh database:

```bash
# Create a blank database first
createdb brain_restored

# Restore
pg_restore \
  --dbname="$DATABASE_URL" \
  --format=custom \
  --no-owner \
  --no-acl \
  --single-transaction \
  brain-backup-*.dump
```

Restore a single table (e.g. `facts`) without touching the rest:

```bash
pg_restore \
  --dbname="$DATABASE_URL" \
  --table=facts \
  --format=custom \
  --no-owner \
  --data-only \
  brain-backup-*.dump
```

---

## Data export via the API (GDPR portability)

Any workspace admin can download a portable JSON export of their data:

```
GET /api/account/export
Authorization: Bearer <admin-token>
```

The response streams as `export-{workspace_id}.json` and contains:

- `teams` — team definitions
- `episodes` — source documents ingested into the brain
- `facts` — extracted knowledge (without vector embeddings)
- `fact_edges` — supersedes / conflict relationships between facts
- `ledger` — query audit log

The export **never includes**: API keys, passwords, other workspaces' data, or vector embeddings (use pg_dump for a full DB export that includes embeddings).

---

## Running migrations on restore

After restoring to a new database, run pending Alembic migrations to ensure the schema is current:

```bash
DATABASE_URL="$DATABASE_URL" alembic upgrade head
```

Check RLS policies are in place after restore:

```bash
DATABASE_URL="$DATABASE_URL" python scripts/check_rls.py
```

A non-zero exit code means one or more tables are missing RLS — re-run the migration or apply policies manually before accepting traffic.
