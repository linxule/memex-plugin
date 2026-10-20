-- Memex cloud mirror schema (D1 / SQLite).
--
-- The local vault stays the source of truth. These tables are a projection
-- of it, filled by `memex cloud push`, plus an inbox for memos written by
-- remote agents that `memex cloud pull` lands back in the vault.
-- Vectors live in Vectorize, keyed by chunks.id / observations.id.

CREATE TABLE docs (
  id INTEGER PRIMARY KEY,             -- rowid shared with fts_docs
  path TEXT NOT NULL UNIQUE,          -- vault-relative, e.g. projects/x/memos/2026-10-19-y.md
  title TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT '',      -- memo | project | topic | transcript | ...
  project TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL DEFAULT '',      -- YYYY-MM-DD or ''
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'vault', -- vault | inbox
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_docs_project_date ON docs(project, date);
CREATE INDEX idx_docs_date ON docs(date);

CREATE VIRTUAL TABLE fts_docs USING fts5(
  title, content, tokenize = 'porter unicode61'
);

-- Chunk text for vector hits (snippets). One row per Vectorize vector.
CREATE TABLE chunks (
  id TEXT PRIMARY KEY,                -- Vectorize id: c:<sha(path)[:40]>:<index>
  doc_path TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL
);
CREATE INDEX idx_chunks_doc ON chunks(doc_path);

CREATE TABLE observations (
  rid INTEGER PRIMARY KEY,            -- rowid shared with fts_observations
  id TEXT NOT NULL UNIQUE,            -- Vectorize id: o:<content_hash[:60]>
  doc_path TEXT NOT NULL,
  content TEXT NOT NULL,
  obs_type TEXT NOT NULL DEFAULT 'explicit',
  confidence TEXT NOT NULL DEFAULT 'high',
  created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_obs_doc ON observations(doc_path);

CREATE VIRTUAL TABLE fts_observations USING fts5(
  content, tokenize = 'porter unicode61'
);

CREATE TABLE observation_topics (
  observation_id TEXT NOT NULL,
  topic_slug TEXT NOT NULL,
  PRIMARY KEY (observation_id, topic_slug)
);
CREATE INDEX idx_obs_topics_slug ON observation_topics(topic_slug);

-- Memos saved by remote agents, waiting for `memex cloud pull`.
CREATE TABLE inbox (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  client TEXT NOT NULL DEFAULT '',
  pulled_at TEXT
);

-- Key/value facts about the last push: embedding provider/model/dimensions.
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Fingerprint of the observation set last pushed per document, so a push
-- only re-sends documents whose observations changed.
CREATE TABLE obs_sets (
  doc_path TEXT PRIMARY KEY,
  set_hash TEXT NOT NULL
);
