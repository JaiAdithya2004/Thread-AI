-- MVP uses SQLite for zero-config. Hosted version: same shape on Postgres+pgvector
-- (acl TEXT[] -> text[], embedding -> vector(1536), add per-workspace row security).

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  source TEXT, external_id TEXT, title TEXT, body TEXT,
  author TEXT, url TEXT, occurred_at TIMESTAMP,
  content_hash TEXT, acl TEXT,           -- acl = comma-joined principals
  team_id TEXT DEFAULT 'shared',         -- which team owns this episode
  extracted INTEGER DEFAULT 0,
  UNIQUE(workspace_id, source, external_id, content_hash)
);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  subject TEXT, predicate TEXT, object TEXT, summary TEXT,
  episode_id TEXT REFERENCES episodes(id),
  acl TEXT, confidence REAL,
  introduced_at TIMESTAMP, invalidated_at TIMESTAMP,
  source_kind TEXT,                      -- 'llm' | 'human'
  team_id TEXT DEFAULT 'shared',         -- inherited from episode
  visibility TEXT DEFAULT 'team'         -- 'team' | 'shared'
);

CREATE TABLE IF NOT EXISTS fact_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT,
  src_fact TEXT, dst_fact TEXT,
  type TEXT                              -- supersedes | in_tension_with | derived_from
);

CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts
  USING fts5(fact_id, summary, subject, object);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT, user_id TEXT, ts TIMESTAMP,
  query TEXT, surface TEXT, fact_ids TEXT, context_sent TEXT,
  team_id TEXT
);
